"""Read model: ``rpt_*`` views. The agent service reads ONLY these (read-only DB user); all writes
go through
the owning services. Every view exposes ``business_id`` and callers must always filter on it.

Definitions are portable SQL (MySQL 8 and SQLite). Metric definitions are part of the contract:
* a *sale* is an order whose status is CONFIRMED, PACKED, SHIPPED, DELIVERED, RETURN_REQUESTED or
RETURNED
  (not PENDING_PAYMENT, not CANCELLED); ``revenue`` is gross ``grand_total`` of those orders;
* ``available`` is ``on_hand - reserved`` summed across warehouses.
"""

from sqlalchemy import event
from sqlalchemy.engine import Connection

from app.db.session import Base

SALE_STATUSES = "('CONFIRMED','PACKED','SHIPPED','DELIVERED','RETURN_REQUESTED','RETURNED')"

VIEWS: dict[str, str] = {
    "rpt_catalog_public": """
        SELECT p.business_id AS business_id, p.id AS product_id, v.id AS variant_id,
               p.name AS product_name, p.slug AS product_slug, c.name AS category, p.brand AS brand,
               v.sku AS sku, v.options AS options, v.selling_price AS price, v.mrp AS mrp,
               p.stock_unit AS stock_unit, p.tax_class AS tax_class,
               (SELECT COALESCE(SUM(b.on_hand - b.reserved), 0) FROM inventory_balances b
                 WHERE b.business_id = v.business_id AND b.variant_id = v.id) AS available_qty
        FROM products p
        JOIN product_variants v ON v.product_id = p.id AND v.business_id = p.business_id
        LEFT JOIN categories c ON c.id = p.category_id AND c.business_id = p.business_id
        WHERE p.status = 'PUBLISHED' AND p.archived_at IS NULL
          AND v.is_active = 1 AND v.archived_at IS NULL
    """,
    "rpt_inventory_current": """
        SELECT v.business_id AS business_id, v.id AS variant_id, p.id AS product_id,
               p.name AS product_name, p.status AS product_status, v.is_active AS is_active,
               v.sku AS sku, v.options AS options, c.name AS category, p.stock_unit AS stock_unit,
               v.reorder_threshold AS reorder_threshold, v.selling_price AS price, v.cost_price AS
               cost_price,
               COALESCE(s.on_hand, 0) AS on_hand, COALESCE(s.reserved, 0) AS reserved,
               COALESCE(s.on_hand, 0) - COALESCE(s.reserved, 0) AS available,
               COALESCE(s.stock_value, 0) AS stock_value
        FROM product_variants v
        JOIN products p ON p.id = v.product_id AND p.business_id = v.business_id
        LEFT JOIN categories c ON c.id = p.category_id AND c.business_id = p.business_id
        LEFT JOIN (SELECT business_id, variant_id, SUM(on_hand) AS on_hand, SUM(reserved) AS
        reserved,
                          SUM(on_hand * avg_cost) AS stock_value
                   FROM inventory_balances GROUP BY business_id, variant_id) s
               ON s.business_id = v.business_id AND s.variant_id = v.id
    """,
    "rpt_low_stock": """
        SELECT * FROM rpt_inventory_current
        WHERE product_status = 'PUBLISHED' AND is_active = 1 AND available <= reorder_threshold
    """,
    "rpt_order_lines": """
        SELECT o.business_id AS business_id, o.id AS order_id, o.order_number AS order_number,
               o.customer_id AS customer_id, o.placed_at AS placed_at, DATE(o.placed_at) AS
               placed_date,
               o.order_status AS order_status, o.payment_status AS payment_status,
               o.fulfilment_status AS fulfilment_status, o.payment_method AS payment_method,
               o.place_of_supply_state AS place_of_supply_state,
               i.product_id AS product_id, i.variant_id AS variant_id, i.product_name AS
               product_name,
               i.sku AS sku, i.options AS options, i.quantity AS quantity, i.unit_price AS
               unit_price,
               i.discount AS discount, i.taxable_value AS taxable_value,
               (i.cgst + i.sgst + i.igst + i.cess) AS tax, i.line_total AS line_total
        FROM orders o JOIN order_items i ON i.order_id = o.id AND i.business_id = o.business_id
    """,
    "rpt_sales_daily": f"""
        SELECT o.business_id AS business_id, DATE(o.placed_at) AS sales_date, COUNT(*) AS orders,
               SUM(o.grand_total) AS revenue, SUM(o.tax_total) AS tax, SUM(o.discount_total) AS
               discounts,
               SUM(COALESCE(u.units, 0)) AS units
        FROM orders o
        LEFT JOIN (SELECT order_id, SUM(quantity) AS units FROM order_items GROUP BY order_id) u
               ON u.order_id = o.id
        WHERE o.order_status IN {SALE_STATUSES}
        GROUP BY o.business_id, DATE(o.placed_at)
    """,
    "rpt_purchasing": """
        SELECT po.business_id AS business_id, po.id AS purchase_order_id, po.number AS number,
               po.status AS status, po.expected_date AS expected_date, po.total AS total,
               s.name AS supplier, s.lead_time_days AS lead_time_days,
               CASE WHEN po.status IN ('APPROVED', 'PARTIALLY_RECEIVED')
                     AND po.expected_date < CURRENT_DATE THEN 1 ELSE 0 END AS is_overdue
        FROM purchase_orders po JOIN suppliers s ON s.id = po.supplier_id AND s.business_id =
        po.business_id
    """,
    "rpt_invoices": """
        SELECT business_id, id AS invoice_id, display_number, doc_type, status, issued_at,
        customer_id,
               order_id, taxable_total, cgst_total, sgst_total, igst_total, cess_total, round_off,
               grand_total, is_inter_state
        FROM invoices
    """,
    # stock already on its way: open purchase-order lines per variant (used by reorder advice)
    "rpt_incoming_stock": """
        SELECT po.business_id AS business_id, i.variant_id AS variant_id,
               SUM(i.quantity - i.received_quantity) AS incoming_qty,
               SUM(CASE WHEN po.expected_date < CURRENT_DATE THEN i.quantity - i.received_quantity
                        ELSE 0 END) AS delayed_qty,
               MAX(s.lead_time_days) AS lead_time_days, MIN(s.name) AS supplier
        FROM purchase_orders po
        JOIN purchase_order_items i ON i.purchase_order_id = po.id AND i.business_id = po.business_id
        JOIN suppliers s ON s.id = po.supplier_id AND s.business_id = po.business_id
        WHERE po.status IN ('APPROVED', 'PARTIALLY_RECEIVED')
        GROUP BY po.business_id, i.variant_id
    """,
    "rpt_customer_signups": """
        SELECT business_id, DATE(created_at) AS signup_date, COUNT(*) AS signups
        FROM customers WHERE disabled_at IS NULL GROUP BY business_id, DATE(created_at)
    """,
}
# creation order matters: rpt_low_stock selects from rpt_inventory_current
VIEW_ORDER = (
    "rpt_catalog_public",
    "rpt_inventory_current",
    "rpt_low_stock",
    "rpt_order_lines",
    "rpt_sales_daily",
    "rpt_purchasing",
    "rpt_invoices",
    "rpt_customer_signups",
    "rpt_incoming_stock",
)


def install_views(connection: Connection) -> None:
    for name in reversed(VIEW_ORDER):
        connection.exec_driver_sql(f"DROP VIEW IF EXISTS {name}")
    for name in VIEW_ORDER:
        connection.exec_driver_sql(f"CREATE VIEW {name} AS {VIEWS[name]}")


def drop_views(connection: Connection) -> None:
    for name in reversed(VIEW_ORDER):
        connection.exec_driver_sql(f"DROP VIEW IF EXISTS {name}")


@event.listens_for(Base.metadata, "after_create")
def _install_after_create(target, connection, **kwargs) -> None:  # pragma: no cover
    install_views(connection)
