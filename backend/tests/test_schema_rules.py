"""Database-level guarantees (docs/03, 06): append-only ledger, immutable invoices, CHECKs,
tenant-scoped
uniqueness, and the one-owner-per-table rule. Runs on SQLite always and on MySQL with
BLISSBIZ_TEST_MYSQL=1."""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import DBAPIError

import app.db.models as models
from app.db.models import Business, Owner
from app.db.models.billing import Invoice, InvoiceLine
from app.db.models.catalog import Product, ProductVariant
from app.db.models.supply import InventoryBalance, InventoryTransaction, Warehouse
from app.db.session import Base
from app.db.views import VIEW_ORDER


def make_business(db) -> Business:
    unique = uuid4().hex
    owner = Owner(full_name="O", email=f"{unique}@example.com", password_hash="x")
    db.add(owner)
    db.flush()
    business = Business(owner_id=owner.id, name="B", slug=f"b-{unique[:20]}")
    db.add(business)
    db.flush()
    return business


def make_variant(db, business: Business, sku="SKU-1") -> ProductVariant:
    product = Product(
        business_id=business.id,
        name="Tee",
        slug=f"tee-{uuid4().hex[:8]}",
        status="PUBLISHED",
        tax_class="GST_5",
    )
    db.add(product)
    db.flush()
    variant = ProductVariant(
        business_id=business.id,
        product_id=product.id,
        sku=sku,
        options={},
        option_hash=uuid4().hex,
        selling_price=Decimal("100"),
        cost_price=Decimal("50"),
    )
    db.add(variant)
    db.flush()
    return variant


def make_warehouse(db, business: Business) -> Warehouse:
    warehouse = Warehouse(business_id=business.id, code="MAIN", name="Main", is_default=True)
    db.add(warehouse)
    db.flush()
    return warehouse


def ledger_row(
    business, variant, warehouse, kind="RECEIPT", delta="5", key=None
) -> InventoryTransaction:
    return InventoryTransaction(
        business_id=business.id,
        variant_id=variant.id,
        warehouse_id=warehouse.id,
        txn_type=kind,
        quantity_delta=Decimal(delta),
        reason_code="TEST",
        source_type="OPENING",
        idempotency_key=key or uuid4().hex,
        actor_type="SYSTEM",
        created_at=datetime(2026, 1, 1),
    )


def blocked(db, statement):
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(statement)


def test_ledger_is_append_only(db):
    b = make_business(db)
    row = ledger_row(b, make_variant(db, b), make_warehouse(db, b))
    db.add(row)
    db.flush()
    blocked(
        db,
        update(InventoryTransaction)
        .where(InventoryTransaction.id == row.id)
        .values(quantity_delta=9),
    )
    blocked(db, text("DELETE FROM inventory_transactions WHERE id = :i").bindparams(i=row.id))


def test_ledger_sign_must_match_type_and_zero_is_rejected(db):
    b = make_business(db)
    v, w = make_variant(db, b), make_warehouse(db, b)
    for kind, delta in (("SALE", "5"), ("RECEIPT", "-5"), ("ADJUSTMENT", "0")):
        with pytest.raises(DBAPIError):
            with db.begin_nested():
                db.add(ledger_row(b, v, w, kind, delta))


def test_ledger_idempotency_key_is_unique_per_business(db):
    b = make_business(db)
    v, w = make_variant(db, b), make_warehouse(db, b)
    db.add(ledger_row(b, v, w, key="same"))
    db.flush()
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.add(ledger_row(b, v, w, key="same"))
    other = make_business(db)  # same key in another business is fine
    db.add(ledger_row(other, make_variant(db, other), make_warehouse(db, other), key="same"))
    db.flush()


@pytest.mark.parametrize("on_hand,reserved", [("-1", "0"), ("5", "-1"), ("5", "6")])
def test_balance_checks_reject_impossible_stock(db, on_hand, reserved):
    b = make_business(db)
    v, w = make_variant(db, b), make_warehouse(db, b)
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.add(
                InventoryBalance(
                    business_id=b.id,
                    variant_id=v.id,
                    warehouse_id=w.id,
                    on_hand=Decimal(on_hand),
                    reserved=Decimal(reserved),
                )
            )


def test_sku_is_unique_per_business_not_globally(db):
    a, b = make_business(db), make_business(db)
    make_variant(db, a, "SHARED")
    make_variant(db, b, "SHARED")
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            make_variant(db, a, "SHARED")


def make_invoice(db, business: Business) -> Invoice:
    invoice = Invoice(
        business_id=business.id,
        order_id=uuid4().hex,
        doc_type="TAX_INVOICE",
        series="TC",
        fy="2026-27",
        number=1,
        display_number="TC/26-27/00001",
        status="ISSUED",
        issued_at=datetime(2026, 11, 1),
        seller={"name": "S"},
        buyer={"name": "B"},
        taxable_total=Decimal("100"),
        grand_total=Decimal("105"),
        content_hash="h",
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            business_id=business.id,
            invoice_id=invoice.id,
            description="x",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            taxable_value=Decimal("100"),
            line_total=Decimal("105"),
        )
    )
    db.flush()
    return invoice


def test_issued_invoice_cannot_be_changed_or_deleted(db):
    invoice = make_invoice(db, make_business(db))
    blocked(db, update(Invoice).where(Invoice.id == invoice.id).values(grand_total=Decimal("1")))
    blocked(db, update(Invoice).where(Invoice.id == invoice.id).values(buyer={"name": "Mallory"}))
    blocked(db, text("DELETE FROM invoices WHERE id = :i").bindparams(i=invoice.id))
    blocked(
        db,
        update(InvoiceLine)
        .where(InvoiceLine.invoice_id == invoice.id)
        .values(quantity=Decimal("9")),
    )


def test_invoice_pdf_and_cancellation_are_the_only_allowed_changes(db):
    invoice = make_invoice(db, make_business(db))
    db.execute(
        update(Invoice)
        .where(Invoice.id == invoice.id)
        .values(pdf_storage_path="/p.pdf", status="CANCELLED")
    )
    db.flush()


def test_every_table_has_exactly_one_owning_service():
    assert set(models.OWNERS) == set(Base.metadata.tables)
    assert set(models.OWNERS.values()) == {
        "identity",
        "store",
        "catalog",
        "supply",
        "sales",
        "billing",
        "agent",
        "notification",
        "platform",
    }


def test_no_cross_service_foreign_keys_except_the_tenant_anchor():
    offenders = []
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            source, target = models.OWNERS[table.name], models.OWNERS[fk.column.table.name]
            if source != target and fk.column.table.name != "businesses":
                offenders.append(f"{table.name}.{fk.parent.name} -> {fk.column.table.name}")
    assert not offenders, offenders


def test_every_tenant_table_carries_business_id():
    exempt = {
        "owners",
        "businesses",
        "browser_sessions",
        "rate_limit_buckets",
        "onboarding_drafts",
        "onboarding_messages",
        "tax_rate_versions",
        "processed_events",
        "outbox_events",
        "idempotency_keys",
        "jobs",
        "stock_adjustment_lines",
        "deployment_logs",
        "health_checks",
        "agent_messages",
    }
    missing = [
        t
        for t, table in Base.metadata.tables.items()
        if "business_id" not in table.c and t not in exempt
    ]
    # tables that hang off a parent carrying business_id are fine; list them explicitly so new
    # ones are reviewed
    assert set(missing) == set(), missing


def test_read_views_exist_and_expose_business_id(db):
    inspector = inspect(db.connection())
    for view in VIEW_ORDER:
        columns = {c["name"] for c in inspector.get_columns(view)}
        assert "business_id" in columns, view
    assert db.execute(text("SELECT COUNT(*) FROM rpt_sales_daily")).scalar() >= 0


def test_views_never_mix_tenants(db):
    a, b = make_business(db), make_business(db)
    make_variant(db, a, "A-ONLY")
    rows = db.execute(
        text("SELECT sku FROM rpt_inventory_current WHERE business_id = :b").bindparams(b=b.id)
    ).all()
    assert rows == []
    rows = db.execute(
        text("SELECT sku FROM rpt_inventory_current WHERE business_id = :b").bindparams(b=a.id)
    ).all()
    assert [r[0] for r in rows] == ["A-ONLY"]
