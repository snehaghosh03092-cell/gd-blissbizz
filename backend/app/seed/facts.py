"""Ground-truth answers computed from the database through the rpt_* views (the same read model the agents use).

``python -m app.seed --facts`` prints them; use them as the answer key when testing voice/insights agents.
"Today" is the date of the newest order, so the report stays right if you seed with --anchor.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine

SALE = "('CONFIRMED','PACKED','SHIPPED','DELIVERED','RETURN_REQUESTED','RETURNED')"


def _rows(conn, sql: str, **params):
    return conn.execute(text(sql), params).all()


def collect(engine: Engine) -> list[dict]:
    out = []
    with engine.connect() as conn:
        ref = conn.execute(text("SELECT MAX(sales_date) FROM rpt_sales_daily")).scalar()
        if ref is None:
            return out
        ref = ref if not isinstance(ref, str) else __import__("datetime").date.fromisoformat(ref)
        d1, d7, d14, d30, d60 = ((ref - timedelta(days=n)).isoformat() for n in (1, 7, 14, 30, 60))
        for biz in _rows(
            conn, "SELECT id, name, slug FROM businesses WHERE status='ACTIVE' ORDER BY slug"
        ):
            b = biz.id
            if not conn.execute(
                text("SELECT COUNT(*) FROM orders WHERE business_id=:b"), {"b": b}
            ).scalar():
                continue

            def one(sql, **p):
                return conn.execute(text(sql), {"b": b, **p}).scalar()

            def many(sql, **p):
                return [tuple(r) for r in conn.execute(text(sql), {"b": b, **p}).all()]

            f = {"name": biz.name, "slug": biz.slug, "today": str(ref)}
            f["variants_published"] = one(
                "SELECT COUNT(*) FROM rpt_catalog_public WHERE business_id=:b"
            )
            f["products_by_status"] = dict(
                many("SELECT status, COUNT(*) FROM products WHERE business_id=:b GROUP BY status")
            )
            f["customers"] = one("SELECT COUNT(*) FROM customers WHERE business_id=:b")
            f["signups_last_7_days"] = one(
                "SELECT COALESCE(SUM(signups),0) FROM rpt_customer_signups WHERE business_id=:b AND signup_date > :d",
                d=d7,
            )
            f["orders_total"] = one("SELECT COUNT(*) FROM orders WHERE business_id=:b")
            f["order_status"] = dict(
                many(
                    "SELECT order_status, COUNT(*) FROM orders WHERE business_id=:b GROUP BY order_status"
                )
            )
            f["payment_method"] = dict(
                many(
                    "SELECT payment_method, COUNT(*) FROM orders WHERE business_id=:b GROUP BY payment_method"
                )
            )
            f["sales_yesterday"] = many(
                "SELECT orders, revenue, units FROM rpt_sales_daily WHERE business_id=:b AND sales_date=:d",
                d=d1,
            )
            f["revenue_last_7_days"] = one(
                "SELECT COALESCE(SUM(revenue),0) FROM rpt_sales_daily WHERE business_id=:b AND sales_date > :d",
                d=d7,
            )
            f["revenue_prev_7_days"] = one(
                "SELECT COALESCE(SUM(revenue),0) FROM rpt_sales_daily WHERE business_id=:b AND sales_date > :a AND sales_date <= :b2",
                a=d14,
                b2=d7,
            )
            f["revenue_last_30_days"] = one(
                "SELECT COALESCE(SUM(revenue),0) FROM rpt_sales_daily WHERE business_id=:b AND sales_date > :d",
                d=d30,
            )
            f["top_products_30d"] = many(
                f"SELECT product_name, SUM(quantity) u FROM rpt_order_lines WHERE business_id=:b AND order_status IN {SALE} AND placed_date > :d GROUP BY product_name ORDER BY u DESC, product_name LIMIT 5",
                d=d30,
            )
            f["top_category_30d"] = many(
                f"SELECT c.name, SUM(l.line_total) r FROM rpt_order_lines l JOIN products p ON p.id=l.product_id JOIN categories c ON c.id=p.category_id WHERE l.business_id=:b AND l.order_status IN {SALE} AND l.placed_date > :d GROUP BY c.name ORDER BY r DESC LIMIT 3",
                d=d30,
            )
            f["out_of_stock_published"] = many(
                "SELECT product_name, sku FROM rpt_inventory_current WHERE business_id=:b AND product_status='PUBLISHED' AND is_active=1 AND available <= 0 ORDER BY product_name, sku"
            )
            f["low_stock_count"] = one("SELECT COUNT(*) FROM rpt_low_stock WHERE business_id=:b")
            f["low_stock_sample"] = many(
                "SELECT product_name, sku, available, reorder_threshold FROM rpt_low_stock WHERE business_id=:b AND available > 0 ORDER BY available, product_name LIMIT 5"
            )
            f["dead_stock_60d"] = many(
                f"SELECT i.product_name, SUM(i.on_hand) FROM rpt_inventory_current i WHERE i.business_id=:b AND i.product_status='PUBLISHED' AND i.on_hand > 0 AND NOT EXISTS (SELECT 1 FROM rpt_order_lines l WHERE l.business_id=i.business_id AND l.variant_id=i.variant_id AND l.order_status IN {SALE} AND l.placed_date > :d) GROUP BY i.product_name ORDER BY i.product_name",
                d=d60,
            )
            f["stock_value_at_cost"] = one(
                "SELECT COALESCE(SUM(stock_value),0) FROM rpt_inventory_current WHERE business_id=:b"
            )
            f["overdue_purchase_orders"] = many(
                "SELECT number, supplier, expected_date FROM rpt_purchasing WHERE business_id=:b AND is_overdue=1 ORDER BY expected_date"
            )
            f["supplier_payments_overdue"] = many(
                "SELECT COUNT(*), COALESCE(SUM(amount-amount_paid),0) FROM supplier_payments WHERE business_id=:b AND status <> 'PAID' AND due_date < :d",
                d=ref.isoformat(),
            )[0]
            f["invoices_by_type"] = dict(
                many(
                    "SELECT doc_type, COUNT(*) FROM rpt_invoices WHERE business_id=:b GROUP BY doc_type"
                )
            )
            f["inter_state_invoices"] = one(
                "SELECT COUNT(*) FROM rpt_invoices WHERE business_id=:b AND is_inter_state=1"
            )
            f["credit_notes"] = one("SELECT COUNT(*) FROM credit_notes WHERE business_id=:b")
            f["refunds_total"] = one(
                "SELECT COALESCE(SUM(amount),0) FROM refunds WHERE business_id=:b"
            )
            f["active_reservations"] = one(
                "SELECT COUNT(*) FROM stock_reservations WHERE business_id=:b AND status='ACTIVE'"
            )
            f["transfers_in_transit"] = one(
                "SELECT COUNT(*) FROM stock_transfers WHERE business_id=:b AND status='IN_TRANSIT'"
            )
            out.append(f)
    return out


def print_facts(engine: Engine) -> None:
    for f in collect(engine):
        print(f"\n=== {f['name']} ({f['slug']}) — 'today' = {f['today']}")
        for key, value in f.items():
            if key not in ("name", "slug", "today"):
                print(f"  {key}: {value}")
