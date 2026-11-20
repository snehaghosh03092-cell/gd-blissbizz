"""OWNER SERVICE: supply. Stock ledger, balances, reservations, purchasing. The only writer of
stock.

* ``inventory_transactions`` is the append-only source of truth (trigger-enforced);
* ``inventory_balances`` is a cache maintained in the same transaction;
* ``variant_id`` / ``order_id`` are plain columns (catalog / sales own those rows), validated
through their APIs.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models._common import (
    BIGPK,
    COST,
    MONEY,
    QTY,
    business_fk,
    enum_check,
    id_column,
    utc_now,
)
from app.db.session import Base

TXN_TYPES = (
    "OPENING",
    "RECEIPT",
    "SALE",
    "CUSTOMER_RETURN",
    "TRANSFER_OUT",
    "TRANSFER_IN",
    "DAMAGE",
    "ADJUSTMENT",
    "PURCHASE_RETURN",
)
INBOUND_TXN_TYPES = ("OPENING", "RECEIPT", "TRANSFER_IN", "CUSTOMER_RETURN")
OUTBOUND_TXN_TYPES = ("SALE", "DAMAGE", "TRANSFER_OUT", "PURCHASE_RETURN")
RESERVATION_STATUSES = ("ACTIVE", "COMMITTED", "RELEASED", "EXPIRED")


class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (UniqueConstraint("business_id", "code", name="uq_warehouses_business_code"),)

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(Text())
    state_code: Mapped[str | None] = mapped_column(String(2))
    is_default: Mapped[bool] = mapped_column(Boolean(), default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime())


class InventoryBalance(Base):
    __tablename__ = "inventory_balances"
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        CheckConstraint("reserved <= on_hand", name="reserved_within_on_hand"),
    )

    business_id: Mapped[str] = business_fk(primary_key=True)
    variant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    warehouse_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("warehouses.id"), primary_key=True
    )
    on_hand: Mapped[Decimal] = mapped_column(QTY, default=0)
    reserved: Mapped[Decimal] = mapped_column(QTY, default=0)
    avg_cost: Mapped[Decimal] = mapped_column(COST, default=0)
    version: Mapped[int] = mapped_column(BIGPK, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class InventoryTransaction(Base):
    """Append-only ledger (UPDATE and DELETE are rejected by triggers)."""

    __tablename__ = "inventory_transactions"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_inventory_txn_business_key"),
        enum_check("txn_type", TXN_TYPES, "txn_type"),
        CheckConstraint(
            "(txn_type IN ('OPENING','RECEIPT','TRANSFER_IN','CUSTOMER_RETURN')"
            " AND quantity_delta > 0)"
            " OR (txn_type IN ('SALE','DAMAGE','TRANSFER_OUT','PURCHASE_RETURN')"
            " AND quantity_delta < 0)"
            " OR (txn_type = 'ADJUSTMENT' AND quantity_delta <> 0)",
            name="delta_sign_matches_type",
        ),
        Index("ix_inventory_txn_replay", "business_id", "variant_id", "warehouse_id", "id"),
        Index("ix_inventory_txn_source", "source_type", "source_id"),
        Index("ix_inventory_txn_business_created", "business_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    business_id: Mapped[str] = business_fk()
    variant_id: Mapped[str] = mapped_column(String(36))
    warehouse_id: Mapped[str] = mapped_column(String(36), ForeignKey("warehouses.id"))
    txn_type: Mapped[str] = mapped_column(String(20))
    quantity_delta: Mapped[Decimal] = mapped_column(QTY)
    unit_cost: Mapped[Decimal | None] = mapped_column(COST)
    reason_code: Mapped[str] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(String(500))
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class StockReservation(Base):
    __tablename__ = "stock_reservations"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "idempotency_key", name="uq_stock_reservations_business_key"
        ),
        enum_check("status", RESERVATION_STATUSES, "status"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_stock_reservations_status_expires", "status", "expires_at"),
        Index("ix_stock_reservations_business_order", "business_id", "order_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    order_id: Mapped[str] = mapped_column(String(36))
    variant_id: Mapped[str] = mapped_column(String(36))
    warehouse_id: Mapped[str] = mapped_column(String(36), ForeignKey("warehouses.id"))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE")
    expires_at: Mapped[datetime] = mapped_column(DateTime())
    idempotency_key: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class StockAdjustment(Base):
    __tablename__ = "stock_adjustments"
    __table_args__ = (
        enum_check("status", ("DRAFT", "POSTED"), "status"),
        Index("ix_stock_adjustments_business_created", "business_id", "created_at"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    reason_code: Mapped[str] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")
    created_by: Mapped[str | None] = mapped_column(String(36))
    approved_by: Mapped[str | None] = mapped_column(String(36))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class StockAdjustmentLine(Base):
    __tablename__ = "stock_adjustment_lines"
    __table_args__ = (Index("ix_stock_adjustment_lines_adjustment", "adjustment_id"),)

    id: Mapped[str] = id_column()
    adjustment_id: Mapped[str] = mapped_column(String(36), ForeignKey("stock_adjustments.id"))
    business_id: Mapped[str] = business_fk()
    variant_id: Mapped[str] = mapped_column(String(36))
    warehouse_id: Mapped[str] = mapped_column(String(36), ForeignKey("warehouses.id"))
    counted_qty: Mapped[Decimal | None] = mapped_column(QTY)
    delta_qty: Mapped[Decimal] = mapped_column(QTY)


class StockTransfer(Base):
    __tablename__ = "stock_transfers"
    __table_args__ = (
        enum_check("status", ("DRAFT", "IN_TRANSIT", "RECEIVED", "CANCELLED"), "status"),
        CheckConstraint("from_warehouse_id <> to_warehouse_id", name="different_warehouses"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    from_warehouse_id: Mapped[str] = mapped_column(String(36), ForeignKey("warehouses.id"))
    to_warehouse_id: Mapped[str] = mapped_column(String(36), ForeignKey("warehouses.id"))
    status: Mapped[str] = mapped_column(String(12), default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime())
    received_at: Mapped[datetime | None] = mapped_column(DateTime())


class StockTransferLine(Base):
    __tablename__ = "stock_transfer_lines"
    __table_args__ = (CheckConstraint("quantity > 0", name="quantity_positive"),)

    id: Mapped[str] = id_column()
    transfer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("stock_transfers.id"), index=True
    )
    business_id: Mapped[str] = business_fk()
    variant_id: Mapped[str] = mapped_column(String(36))
    quantity: Mapped[Decimal] = mapped_column(QTY)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[str] = id_column()
    business_id: Mapped[str | None] = business_fk(nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime())
    status: Mapped[str] = mapped_column(String(12), default="RUNNING")
    checked_count: Mapped[int] = mapped_column(Integer(), default=0)
    discrepancy_count: Mapped[int] = mapped_column(Integer(), default=0)


class ReconciliationDiscrepancy(Base):
    """Reported, never auto-fixed: resolution is an audited stock adjustment."""

    __tablename__ = "reconciliation_discrepancies"
    __table_args__ = (
        enum_check(
            "kind",
            ("ON_HAND_MISMATCH", "RESERVED_MISMATCH", "NEGATIVE_AVAILABLE"),
            "kind",
        ),
        Index("ix_reconciliation_discrepancies_run", "run_id"),
    )

    id: Mapped[str] = id_column()
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("reconciliation_runs.id"))
    business_id: Mapped[str] = business_fk()
    variant_id: Mapped[str] = mapped_column(String(36))
    warehouse_id: Mapped[str] = mapped_column(String(36), ForeignKey("warehouses.id"))
    kind: Mapped[str] = mapped_column(String(20))
    expected: Mapped[Decimal] = mapped_column(QTY)
    actual: Mapped[Decimal] = mapped_column(QTY)
    resolved_by: Mapped[str | None] = mapped_column(String(36))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime())
    resolution_adjustment_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("stock_adjustments.id")
    )


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("business_id", "name", name="uq_suppliers_business_name"),)

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    name: Mapped[str] = mapped_column(String(150))
    contact_name: Mapped[str | None] = mapped_column(String(150))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(32))
    gstin: Mapped[str | None] = mapped_column(String(15))
    address: Mapped[str | None] = mapped_column(Text())
    lead_time_days: Mapped[int] = mapped_column(Integer(), default=7)
    payment_terms_days: Mapped[int] = mapped_column(Integer(), default=0)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("business_id", "number", name="uq_purchase_orders_business_number"),
        enum_check(
            "status",
            ("DRAFT", "APPROVED", "PARTIALLY_RECEIVED", "RECEIVED", "CANCELLED"),
            "status",
        ),
        Index(
            "ix_purchase_orders_business_status_expected", "business_id", "status", "expected_date"
        ),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    supplier_id: Mapped[str] = mapped_column(String(36), ForeignKey("suppliers.id"))
    number: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    expected_date: Mapped[date | None] = mapped_column(Date())
    total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("received_quantity >= 0", name="received_non_negative"),
    )

    id: Mapped[str] = id_column()
    purchase_order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_orders.id"), index=True
    )
    business_id: Mapped[str] = business_fk()
    variant_id: Mapped[str] = mapped_column(String(36))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    unit_cost: Mapped[Decimal] = mapped_column(COST)
    received_quantity: Mapped[Decimal] = mapped_column(QTY, default=0)


class GoodsReceipt(Base):
    __tablename__ = "goods_receipts"
    __table_args__ = (
        UniqueConstraint("business_id", "number", name="uq_goods_receipts_business_number"),
        UniqueConstraint("business_id", "idempotency_key", name="uq_goods_receipts_business_key"),
        enum_check("status", ("POSTED", "REVERSED"), "status"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    purchase_order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("purchase_orders.id")
    )
    supplier_id: Mapped[str] = mapped_column(String(36), ForeignKey("suppliers.id"))
    warehouse_id: Mapped[str] = mapped_column(String(36), ForeignKey("warehouses.id"))
    number: Mapped[str] = mapped_column(String(30))
    received_at: Mapped[datetime] = mapped_column(DateTime())
    status: Mapped[str] = mapped_column(String(10), default="POSTED")
    idempotency_key: Mapped[str] = mapped_column(String(100))
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class GoodsReceiptItem(Base):
    __tablename__ = "goods_receipt_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="quantity_positive"),)

    id: Mapped[str] = id_column()
    receipt_id: Mapped[str] = mapped_column(String(36), ForeignKey("goods_receipts.id"), index=True)
    business_id: Mapped[str] = business_fk()
    variant_id: Mapped[str] = mapped_column(String(36))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    unit_cost: Mapped[Decimal] = mapped_column(COST)


class Document(Base):
    """Immutable originals (supplier invoices etc.); a corrected version is a new row."""

    __tablename__ = "documents"
    __table_args__ = (enum_check("kind", ("PURCHASE_INVOICE", "OTHER"), "kind"),)

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    kind: Mapped[str] = mapped_column(String(20))
    storage_path: Mapped[str] = mapped_column(String(512))
    original_name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(80))
    byte_size: Mapped[int] = mapped_column(Integer())
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class PurchaseInvoice(Base):
    """Posts to payables only when APPROVED by a human; AI extraction only ever creates DRAFTs."""

    __tablename__ = "purchase_invoices"
    __table_args__ = (
        enum_check("status", ("DRAFT", "NEEDS_REVIEW", "APPROVED", "REJECTED"), "status"),
        Index("ix_purchase_invoices_business_status", "business_id", "status"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    supplier_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("suppliers.id"))
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"))
    status: Mapped[str] = mapped_column(String(14), default="DRAFT")
    extraction: Mapped[dict | None] = mapped_column(JSON)
    field_confidence: Mapped[dict | None] = mapped_column(JSON)
    supplier_invoice_number: Mapped[str | None] = mapped_column(String(60))
    invoice_date: Mapped[date | None] = mapped_column(Date())
    due_date: Mapped[date | None] = mapped_column(Date())
    taxable_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    tax_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    grand_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    linked_po_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("purchase_orders.id"))
    approved_by: Mapped[str | None] = mapped_column(String(36))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime())
    correction_history: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class PurchaseReturn(Base):
    __tablename__ = "purchase_returns"
    __table_args__ = (
        UniqueConstraint("business_id", "number", name="uq_purchase_returns_business_number"),
        enum_check("status", ("DRAFT", "POSTED"), "status"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    supplier_id: Mapped[str] = mapped_column(String(36), ForeignKey("suppliers.id"))
    warehouse_id: Mapped[str] = mapped_column(String(36), ForeignKey("warehouses.id"))
    number: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class PurchaseReturnItem(Base):
    __tablename__ = "purchase_return_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="quantity_positive"),)

    id: Mapped[str] = id_column()
    return_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_returns.id"), index=True
    )
    business_id: Mapped[str] = business_fk()
    variant_id: Mapped[str] = mapped_column(String(36))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    unit_cost: Mapped[Decimal] = mapped_column(COST)


class PurchaseDebitNote(Base):
    __tablename__ = "purchase_debit_notes"
    __table_args__ = (
        UniqueConstraint("business_id", "number", name="uq_purchase_debit_notes_number"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    supplier_id: Mapped[str] = mapped_column(String(36), ForeignKey("suppliers.id"))
    purchase_return_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("purchase_returns.id")
    )
    purchase_invoice_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("purchase_invoices.id")
    )
    number: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class SupplierPayment(Base):
    """Recorded manually; no automatic bank payments."""

    __tablename__ = "supplier_payments"
    __table_args__ = (
        enum_check("status", ("DUE", "PARTIAL", "PAID"), "status"),
        Index("ix_supplier_payments_business_status_due", "business_id", "status", "due_date"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    supplier_id: Mapped[str] = mapped_column(String(36), ForeignKey("suppliers.id"))
    purchase_invoice_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("purchase_invoices.id")
    )
    status: Mapped[str] = mapped_column(String(8), default="DUE")
    amount: Mapped[Decimal] = mapped_column(MONEY)
    amount_paid: Mapped[Decimal] = mapped_column(MONEY, default=0)
    due_date: Mapped[date | None] = mapped_column(Date())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime())
    reference: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
