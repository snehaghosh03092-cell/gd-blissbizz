"""Data-integrity checks run after seeding (and by tests). Each check is SQL returning the number of offending
rows; 0 means the invariant holds. These mirror the guarantees in docs/04 and docs/06."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

EPS = "0.005"

CHECKS: dict[str, str] = {
    "ledger sum equals balance on_hand": f"""
        SELECT COUNT(*) FROM inventory_balances b
        LEFT JOIN (SELECT business_id, variant_id, warehouse_id, SUM(quantity_delta) AS s
                   FROM inventory_transactions GROUP BY business_id, variant_id, warehouse_id) t
               ON t.business_id=b.business_id AND t.variant_id=b.variant_id AND t.warehouse_id=b.warehouse_id
        WHERE ABS(COALESCE(t.s,0) - b.on_hand) > {EPS}""",
    "every ledger group has a balance row": """
        SELECT COUNT(*) FROM (SELECT business_id, variant_id, warehouse_id FROM inventory_transactions
                              GROUP BY business_id, variant_id, warehouse_id) t
        LEFT JOIN inventory_balances b ON b.business_id=t.business_id AND b.variant_id=t.variant_id
              AND b.warehouse_id=t.warehouse_id
        WHERE b.variant_id IS NULL""",
    "reserved equals active reservations": f"""
        SELECT COUNT(*) FROM inventory_balances b
        LEFT JOIN (SELECT business_id, variant_id, warehouse_id, SUM(quantity) AS q FROM stock_reservations
                   WHERE status='ACTIVE' GROUP BY business_id, variant_id, warehouse_id) r
               ON r.business_id=b.business_id AND r.variant_id=b.variant_id AND r.warehouse_id=b.warehouse_id
        WHERE ABS(COALESCE(r.q,0) - b.reserved) > {EPS}""",
    "no negative availability": "SELECT COUNT(*) FROM inventory_balances WHERE on_hand < 0 OR reserved < 0 OR reserved > on_hand",
    "order total identity (inclusive vs exclusive pricing)": f"""
        SELECT COUNT(*) FROM orders o JOIN business_tax_profiles p ON p.business_id=o.business_id
        WHERE ABS(o.grand_total - (o.subtotal - o.discount_total + o.shipping_total + o.round_off
                  + CASE WHEN p.prices_include_tax = 1 THEN 0 ELSE o.tax_total END)) > {EPS}""",
    "invoice equals its lines": f"""
        SELECT COUNT(*) FROM invoices i
        JOIN (SELECT invoice_id, SUM(line_total) AS s, SUM(taxable_value) AS t FROM invoice_lines GROUP BY invoice_id) l
          ON l.invoice_id=i.id
        WHERE ABS(i.grand_total - (l.s + i.round_off)) > {EPS} OR ABS(i.taxable_total - l.t) > {EPS}""",
    "invoice equals its order": f"""
        SELECT COUNT(*) FROM invoices i JOIN orders o ON o.id=i.order_id AND o.business_id=i.business_id
        WHERE ABS(i.grand_total - o.grand_total) > {EPS}""",
    "invoice numbers are gapless per business/fy/series": """
        SELECT COUNT(*) FROM (SELECT business_id, fy, series, COUNT(*) AS c, MAX(number) AS m
                              FROM invoices GROUP BY business_id, fy, series) x WHERE x.c <> x.m""",
    "sequence rows match issued numbers": """
        SELECT COUNT(*) FROM invoice_sequences s
        LEFT JOIN (SELECT business_id, fy, series, MAX(number) AS m FROM invoices GROUP BY business_id, fy, series) i
               ON i.business_id=s.business_id AND i.fy=s.fy AND i.series=s.series
        LEFT JOIN (SELECT business_id, fy, series, MAX(number) AS m FROM credit_notes GROUP BY business_id, fy, series) c
               ON c.business_id=s.business_id AND c.fy=s.fy AND c.series=s.series
        WHERE s.last_number <> COALESCE(i.m, c.m, -1)""",
    "credit notes do not exceed their invoice": """
        SELECT COUNT(*) FROM credit_notes c JOIN invoices i ON i.id=c.invoice_id WHERE c.grand_total > i.grand_total""",
    "refunds do not exceed captured payment": """
        SELECT COUNT(*) FROM payments p JOIN (SELECT payment_id, SUM(amount) AS s FROM refunds GROUP BY payment_id) r
          ON r.payment_id=p.id WHERE r.s > p.amount""",
    "children belong to the parent's business (order items)": """
        SELECT COUNT(*) FROM order_items i JOIN orders o ON o.id=i.order_id WHERE i.business_id <> o.business_id""",
    "children belong to the parent's business (variants)": """
        SELECT COUNT(*) FROM product_variants v JOIN products p ON p.id=v.product_id WHERE v.business_id <> p.business_id""",
    "ledger rows use the business's own warehouses": """
        SELECT COUNT(*) FROM inventory_transactions t JOIN warehouses w ON w.id=t.warehouse_id WHERE w.business_id <> t.business_id""",
    "ledger variants exist in the same business": """
        SELECT COUNT(*) FROM inventory_transactions t LEFT JOIN product_variants v
               ON v.id=t.variant_id AND v.business_id=t.business_id WHERE v.id IS NULL""",
    "orders reference customers of the same business": """
        SELECT COUNT(*) FROM orders o LEFT JOIN customers c ON c.id=o.customer_id AND c.business_id=o.business_id
        WHERE c.id IS NULL""",
    "order items reference variants of the same business": """
        SELECT COUNT(*) FROM order_items i LEFT JOIN product_variants v ON v.id=i.variant_id AND v.business_id=i.business_id
        WHERE v.id IS NULL""",
    "invoices only for shipped-or-later orders": """
        SELECT COUNT(*) FROM invoices i JOIN orders o ON o.id=i.order_id
        WHERE o.order_status IN ('PENDING_PAYMENT','CONFIRMED','PACKED','CANCELLED')""",
    "online confirmed orders have a captured payment": """
        SELECT COUNT(*) FROM orders o LEFT JOIN payments p ON p.order_id=o.id AND p.status IN ('CAPTURED','REFUNDED','PARTIALLY_REFUNDED')
        WHERE o.payment_method='ONLINE' AND o.order_status NOT IN ('PENDING_PAYMENT','CANCELLED') AND p.id IS NULL""",
    # one cent of tolerance per line: SQLite rounds floats, MySQL DECIMAL is exact
    "purchase order totals equal item sums": f"""
        SELECT COUNT(*) FROM purchase_orders po JOIN (SELECT purchase_order_id, SUM(ROUND(quantity*unit_cost,2)) AS s,
               COUNT(*) AS n FROM purchase_order_items GROUP BY purchase_order_id) i ON i.purchase_order_id=po.id
        WHERE ABS(po.total - i.s) > 0.01 * i.n + {EPS}""",
    "received never exceeds ordered": "SELECT COUNT(*) FROM purchase_order_items WHERE received_quantity > quantity",
    "returns never exceed ordered quantity": """
        SELECT COUNT(*) FROM return_request_items r JOIN order_items i ON i.id=r.order_item_id WHERE r.quantity > i.quantity""",
    "published products have an active variant": """
        SELECT COUNT(*) FROM products p WHERE p.status='PUBLISHED'
          AND NOT EXISTS (SELECT 1 FROM product_variants v WHERE v.product_id=p.id AND v.is_active=1)""",
    "GST-registered businesses have a GSTIN": """
        SELECT COUNT(*) FROM business_tax_profiles WHERE gst_registered=1 AND gstin IS NULL""",
}


def run_checks(engine: Engine) -> dict[str, int]:
    out: dict[str, int] = {}
    with engine.connect() as conn:
        for name, sql in CHECKS.items():
            out[name] = int(conn.execute(text(sql)).scalar() or 0)
    return out


def violations(engine: Engine) -> dict[str, int]:
    return {name: n for name, n in run_checks(engine).items() if n}
