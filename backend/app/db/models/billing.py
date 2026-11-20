"""OWNER SERVICE: billing. Versioned tax rates, gapless invoice numbering, immutable invoices and
credit notes.

Issued invoices, their lines and credit notes are never updated or deleted (triggers); corrections
are
credit/debit notes. ``order_id`` / ``customer_id`` are plain columns (sales/identity own those
rows).
Tax output is software support, not legal advice: rates are SAMPLE data until a chartered
accountant signs off.
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models._common import MONEY, QTY, RATE, business_fk, enum_check, id_column, utc_now
from app.db.session import Base


class TaxRateVersion(Base):
    """Effective-dated rates per tax class; invoices record which version they used."""

    __tablename__ = "tax_rate_versions"
    __table_args__ = (
        UniqueConstraint("tax_class", "effective_from", name="uq_tax_rate_versions_class_from"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="date_order"
        ),
    )

    id: Mapped[str] = id_column()
    tax_class: Mapped[str] = mapped_column(String(10))
    cgst_rate: Mapped[Decimal] = mapped_column(RATE, default=0)
    sgst_rate: Mapped[Decimal] = mapped_column(RATE, default=0)
    igst_rate: Mapped[Decimal] = mapped_column(RATE, default=0)
    cess_rate: Mapped[Decimal] = mapped_column(RATE, default=0)
    effective_from: Mapped[date] = mapped_column(Date())
    effective_to: Mapped[date | None] = mapped_column(Date())
    source_note: Mapped[str | None] = mapped_column(String(255))


class InvoiceSequence(Base):
    """Row-locked (SELECT ... FOR UPDATE) inside the issuing transaction, so numbers are gapless."""

    __tablename__ = "invoice_sequences"

    business_id: Mapped[str] = business_fk(primary_key=True)
    fy: Mapped[str] = mapped_column(String(7), primary_key=True)  # '2026-27'
    series: Mapped[str] = mapped_column(String(8), primary_key=True)  # 'INV', 'BOS', 'CN'
    last_number: Mapped[int] = mapped_column(Integer(), default=0)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("business_id", "series", "fy", "number", name="uq_invoices_series_number"),
        UniqueConstraint("business_id", "display_number", name="uq_invoices_display_number"),
        UniqueConstraint("business_id", "order_id", name="uq_invoices_business_order"),
        enum_check("doc_type", ("TAX_INVOICE", "BILL_OF_SUPPLY"), "doc_type"),
        enum_check("status", ("ISSUED", "CANCELLED"), "status"),
        Index("ix_invoices_business_issued", "business_id", "issued_at"),
        Index("ix_invoices_business_customer", "business_id", "customer_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    order_id: Mapped[str] = mapped_column(String(36))
    customer_id: Mapped[str | None] = mapped_column(String(36))
    doc_type: Mapped[str] = mapped_column(String(14))
    series: Mapped[str] = mapped_column(String(8))
    fy: Mapped[str] = mapped_column(String(7))
    number: Mapped[int] = mapped_column(Integer())
    display_number: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(10), default="ISSUED")
    issued_at: Mapped[datetime] = mapped_column(DateTime())
    seller: Mapped[dict] = mapped_column(JSON)
    buyer: Mapped[dict] = mapped_column(JSON)
    place_of_supply_state: Mapped[str | None] = mapped_column(String(2))
    is_inter_state: Mapped[bool] = mapped_column(Boolean(), default=False)
    taxable_total: Mapped[Decimal] = mapped_column(MONEY)
    cgst_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    sgst_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    igst_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    cess_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    round_off: Mapped[Decimal] = mapped_column(MONEY, default=0)
    grand_total: Mapped[Decimal] = mapped_column(MONEY)
    prices_include_tax: Mapped[bool] = mapped_column(Boolean(), default=True)
    tax_rate_version_ids: Mapped[list | None] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    pdf_storage_path: Mapped[str | None] = mapped_column(String(512))
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class InvoiceLine(Base):
    """Never updated or deleted (trigger)."""

    __tablename__ = "invoice_lines"
    __table_args__ = (Index("ix_invoice_lines_invoice", "invoice_id"),)

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    invoice_id: Mapped[str] = mapped_column(String(36), ForeignKey("invoices.id"))
    position: Mapped[int] = mapped_column(Integer(), default=1)
    description: Mapped[str] = mapped_column(String(300))
    hsn_sac: Mapped[str | None] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    unit: Mapped[str] = mapped_column(String(10), default="PCS")
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    discount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    taxable_value: Mapped[Decimal] = mapped_column(MONEY)
    cgst_rate: Mapped[Decimal] = mapped_column(RATE, default=0)
    cgst: Mapped[Decimal] = mapped_column(MONEY, default=0)
    sgst_rate: Mapped[Decimal] = mapped_column(RATE, default=0)
    sgst: Mapped[Decimal] = mapped_column(MONEY, default=0)
    igst_rate: Mapped[Decimal] = mapped_column(RATE, default=0)
    igst: Mapped[Decimal] = mapped_column(MONEY, default=0)
    cess_rate: Mapped[Decimal] = mapped_column(RATE, default=0)
    cess: Mapped[Decimal] = mapped_column(MONEY, default=0)
    line_total: Mapped[Decimal] = mapped_column(MONEY)


class CreditNote(Base):
    """The only way to correct or reverse an issued invoice. Never updated or deleted (trigger)."""

    __tablename__ = "credit_notes"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "series", "fy", "number", name="uq_credit_notes_series_number"
        ),
        UniqueConstraint("business_id", "display_number", name="uq_credit_notes_display_number"),
        Index("ix_credit_notes_business_invoice", "business_id", "invoice_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    invoice_id: Mapped[str] = mapped_column(String(36), ForeignKey("invoices.id"))
    series: Mapped[str] = mapped_column(String(8), default="CN")
    fy: Mapped[str] = mapped_column(String(7))
    number: Mapped[int] = mapped_column(Integer())
    display_number: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(255))
    issued_at: Mapped[datetime] = mapped_column(DateTime())
    taxable_total: Mapped[Decimal] = mapped_column(MONEY)
    cgst_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    sgst_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    igst_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    cess_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    round_off: Mapped[Decimal] = mapped_column(MONEY, default=0)
    grand_total: Mapped[Decimal] = mapped_column(MONEY)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class CreditNoteLine(Base):
    __tablename__ = "credit_note_lines"

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    credit_note_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("credit_notes.id"), index=True
    )
    description: Mapped[str] = mapped_column(String(300))
    hsn_sac: Mapped[str | None] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    taxable_value: Mapped[Decimal] = mapped_column(MONEY)
    cgst: Mapped[Decimal] = mapped_column(MONEY, default=0)
    sgst: Mapped[Decimal] = mapped_column(MONEY, default=0)
    igst: Mapped[Decimal] = mapped_column(MONEY, default=0)
    cess: Mapped[Decimal] = mapped_column(MONEY, default=0)
    line_total: Mapped[Decimal] = mapped_column(MONEY)
