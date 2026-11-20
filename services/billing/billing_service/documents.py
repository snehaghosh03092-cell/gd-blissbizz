"""GST invoices and credit notes (docs/04 section 7).

* An invoice is issued once per order, at dispatch, from the order's own snapshot (prices and tax
  were fixed at checkout). The shipping line is re-derived with the same engine and date, then
  the document must add up to the order's grand total exactly, otherwise nothing is issued.
* Numbers are gapless per business, financial year and series: the sequence row is locked
  (``SELECT ... FOR UPDATE``) in the same transaction that inserts the document.
* GST-registered sellers issue TAX_INVOICE; unregistered sellers issue BILL_OF_SUPPLY (no tax).
* Issued documents are immutable (database triggers). Corrections are credit notes, which can
  never add up to more than their invoice.
"""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from app.db.models import utc_now
from app.db.models.billing import (
    CreditNote,
    CreditNoteLine,
    Invoice,
    InvoiceLine,
    InvoiceSequence,
    TaxRateVersion,
)
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from blissbiz_common.fy import document_number, financial_year, india_date
from blissbiz_common.paging import page_query
from blissbiz_common.tax import SHIPPING_SAC, round2, round_rupee
from blissbiz_common.tax import quote as tax_quote
from blissbiz_common.workbooks import XLSX
from fastapi import Depends, FastAPI, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ZERO = Decimal(0)
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
Qty = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=3)]


class Party(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    gstin: str | None = Field(default=None, max_length=15)
    address: str | None = Field(default=None, max_length=1000)
    state_code: str | None = Field(default=None, max_length=2)


class OrderLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(max_length=300)
    hsn_sac: str | None = Field(default=None, max_length=8)
    quantity: Qty
    unit: str = Field(default="PCS", max_length=10)
    unit_price: Money
    discount: Money = ZERO
    taxable: Money
    cgst: Money = ZERO
    sgst: Money = ZERO
    igst: Money = ZERO
    cess: Money = ZERO
    tax_rate_version_id: str | None = Field(default=None, max_length=36)
    line_total: Money


class InvoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(max_length=36)
    customer_id: str | None = Field(default=None, max_length=36)
    placed_on: date
    seller: Party
    buyer: Party
    gst_registered: bool
    prices_include_tax: bool
    invoice_prefix: str = Field(min_length=1, max_length=8, pattern=r"^[A-Z0-9]+$")
    fy_start_month: int = Field(default=4, ge=1, le=12)
    place_of_supply_state: str | None = Field(default=None, max_length=2)
    inter_state: bool
    shipping_total: Money = ZERO
    round_off: Decimal = Field(ge=-1, le=1, max_digits=6, decimal_places=2)
    grand_total: Money
    lines: list[OrderLine] = Field(min_length=1, max_length=100)


class ReturnedLine(BaseModel):
    """One order line being credited. ``already`` = quantity credited by earlier notes."""

    model_config = ConfigDict(extra="forbid")
    description: str = Field(max_length=300)
    hsn_sac: str | None = Field(default=None, max_length=8)
    ordered: Qty
    already: Decimal = Field(default=ZERO, ge=0, max_digits=14, decimal_places=3)
    quantity: Qty
    taxable: Money
    cgst: Money = ZERO
    sgst: Money = ZERO
    igst: Money = ZERO
    cess: Money = ZERO


class CreditNoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(max_length=36)
    source_id: str = Field(max_length=64)  # e.g. the return id: the same source never credits twice
    reason: str = Field(max_length=255)
    fy_start_month: int = Field(default=4, ge=1, le=12)
    lines: list[ReturnedLine] = Field(min_length=1, max_length=100)


def canonical_hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def rate(value) -> str:
    """9.000 -> '9', 2.500 -> '2.5' (rates are shown as written on the invoice)."""
    return f"{Decimal(value).normalize():f}"


def m(value) -> str:
    return f"{value:.2f}"


def invoice_out(inv: Invoice, lines=None, notes=None) -> dict:
    out = {
        "id": inv.id,
        "order_id": inv.order_id,
        "customer_id": inv.customer_id,
        "doc_type": inv.doc_type,
        "number": inv.display_number,
        "fy": inv.fy,
        "status": inv.status,
        "issued_at": inv.issued_at.isoformat(),
        "seller": inv.seller,
        "buyer": inv.buyer,
        "place_of_supply_state": inv.place_of_supply_state,
        "is_inter_state": inv.is_inter_state,
        "prices_include_tax": inv.prices_include_tax,
        "taxable_total": m(inv.taxable_total),
        "cgst_total": m(inv.cgst_total),
        "sgst_total": m(inv.sgst_total),
        "igst_total": m(inv.igst_total),
        "cess_total": m(inv.cess_total),
        "round_off": m(inv.round_off),
        "grand_total": m(inv.grand_total),
        "content_hash": inv.content_hash,
    }
    if lines is not None:
        out["lines"] = [
            {
                "position": ln.position,
                "description": ln.description,
                "hsn_sac": ln.hsn_sac,
                "quantity": f"{ln.quantity.normalize():f}",
                "unit": ln.unit,
                "unit_price": m(ln.unit_price),
                "discount": m(ln.discount),
                "taxable_value": m(ln.taxable_value),
                "cgst_rate": rate(ln.cgst_rate),
                "cgst": m(ln.cgst),
                "sgst_rate": rate(ln.sgst_rate),
                "sgst": m(ln.sgst),
                "igst_rate": rate(ln.igst_rate),
                "igst": m(ln.igst),
                "cess": m(ln.cess),
                "line_total": m(ln.line_total),
            }
            for ln in lines
        ]
    if notes is not None:
        out["credit_notes"] = [credit_note_out(n) for n in notes]
    return out


def credit_note_out(n: CreditNote, lines=None) -> dict:
    out = {
        "id": n.id,
        "invoice_id": n.invoice_id,
        "number": n.display_number,
        "reason": n.reason,
        "issued_at": n.issued_at.isoformat(),
        "taxable_total": m(n.taxable_total),
        "cgst_total": m(n.cgst_total),
        "sgst_total": m(n.sgst_total),
        "igst_total": m(n.igst_total),
        "cess_total": m(n.cess_total),
        "round_off": m(n.round_off),
        "grand_total": m(n.grand_total),
    }
    if lines is not None:
        out["lines"] = [
            {
                "description": ln.description,
                "hsn_sac": ln.hsn_sac,
                "quantity": f"{ln.quantity.normalize():f}",
                "taxable_value": m(ln.taxable_value),
                "cgst": m(ln.cgst),
                "sgst": m(ln.sgst),
                "igst": m(ln.igst),
                "cess": m(ln.cess),
                "line_total": m(ln.line_total),
            }
            for ln in lines
        ]
    return out


def next_number(db: Session, business_id: str, fy: str, series: str) -> int:
    """Gapless: lock the sequence row; create it on first use (a racing creator -> retry)."""
    query = (
        select(InvoiceSequence)
        .where(
            InvoiceSequence.business_id == business_id,
            InvoiceSequence.fy == fy,
            InvoiceSequence.series == series,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    seq = db.scalar(query)
    if seq is None:
        seq = InvoiceSequence(business_id=business_id, fy=fy, series=series, last_number=0)
        db.add(seq)
        db.flush()  # IntegrityError if another transaction created it first: caller retries
    seq.last_number += 1
    return seq.last_number


def proportional(value: Decimal, part: Decimal, whole: Decimal) -> Decimal:
    return round2(value * part / whole)


def install_document_routes(app: FastAPI, *, Ctx, Db, rate_lookup) -> None:
    paging = Depends(page_query())

    def sales_only(ctx: InternalContext) -> str:
        if ctx.actor_type != "SERVICE" or ctx.actor_id != "sales" or not ctx.business_id:
            raise ApiError(403, "forbidden", "You do not have permission to do that.")
        return ctx.business_id

    def business(ctx: InternalContext) -> str:
        if not ctx.business_id:
            raise ApiError(404, "not_found", "Not found.")
        return ctx.business_id

    def may_read(ctx: InternalContext, inv: Invoice) -> None:
        """Owners with invoices.read, the customer it was issued to, or a service."""
        if ctx.actor_type == "SERVICE":
            return
        if ctx.actor_type == "CUSTOMER" and ctx.actor_id and inv.customer_id == ctx.actor_id:
            return
        if ctx.actor_type == "OWNER":
            ctx.require_permission("invoices.read")
            return
        raise ApiError(404, "not_found", "Not found.")

    def full(db: Session, inv: Invoice) -> dict:
        lines = db.scalars(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == inv.id)
            .order_by(InvoiceLine.position)
        ).all()
        notes = db.scalars(
            select(CreditNote).where(CreditNote.invoice_id == inv.id).order_by(CreditNote.issued_at)
        ).all()
        return invoice_out(inv, lines, notes)

    def rates_of(db: Session, version_id: str | None) -> tuple[Decimal, Decimal, Decimal]:
        if not version_id:
            return ZERO, ZERO, ZERO
        row = db.get(TaxRateVersion, version_id)
        return (row.cgst_rate, row.sgst_rate, row.igst_rate) if row else (ZERO, ZERO, ZERO)

    @app.get("/internal/v1/document-exports/{kind}")
    def export_documents(
        kind: Literal["invoices.xlsx", "invoices.zip"],
        ctx: Ctx,
        db: Db,
        start: date | None = Query(default=None, ge=date(1900, 1, 1), le=date(9998, 12, 31)),
        end: date | None = Query(default=None, ge=date(1900, 1, 1), le=date(9998, 12, 31)),
    ):
        from billing_service.exports import invoice_bundle, invoice_workbook, records

        if ctx.actor_type != "OWNER":
            raise ApiError(403, "forbidden", "Owners only.")
        ctx.require_permission("invoices.read")
        items = records(db, business(ctx), start, end)
        packed = kind.endswith(".zip")
        content = (
            invoice_bundle(items, start, end) if packed else invoice_workbook(items, start, end)
        )
        return Response(
            content,
            media_type="application/zip" if packed else XLSX,
            headers={
                "Content-Disposition": f'attachment; filename="{kind}"',
                "Cache-Control": "no-store",
            },
        )

    # ---- invoices ---------------------------------------------------------------------------
    @app.post("/internal/v1/invoices", status_code=201)
    def issue(body: InvoiceIn, ctx: Ctx, db: Db):
        b = sales_only(ctx)
        existing = db.scalar(
            select(Invoice).where(Invoice.business_id == b, Invoice.order_id == body.order_id)
        )
        if existing is not None:  # one invoice per order: a retry gets the same document
            return {**full(db, existing), "replayed": True}
        lines = [ln.model_dump() for ln in body.lines]
        for ln in lines:
            ln["rates"] = rates_of(db, ln["tax_rate_version_id"])
        if body.shipping_total > 0:
            shipping = tax_quote(
                [],
                rates=rate_lookup(db),
                seller_registered=body.gst_registered,
                inter_state=body.inter_state,
                include_tax=body.prices_include_tax,
                shipping_gross=body.shipping_total,
                on=body.placed_on,
            ).shipping
            lines.append(
                {
                    "description": "Shipping and handling",
                    "hsn_sac": SHIPPING_SAC,
                    "quantity": Decimal(1),
                    "unit": "PCS",
                    "unit_price": body.shipping_total,
                    "discount": ZERO,
                    "taxable": shipping.taxable,
                    "cgst": shipping.cgst,
                    "sgst": shipping.sgst,
                    "igst": shipping.igst,
                    "cess": shipping.cess,
                    "tax_rate_version_id": shipping.version_id,
                    "line_total": shipping.line_total,
                    "rates": rates_of(db, shipping.version_id),
                }
            )
        totals = {
            k: sum((ln[k] for ln in lines), ZERO)
            for k in ("taxable", "cgst", "sgst", "igst", "cess", "line_total")
        }
        if totals["line_total"] + body.round_off != body.grand_total:
            raise ApiError(
                409,
                "totals_mismatch",
                "The order's lines do not add up to its total; no invoice issued.",
            )
        if not body.gst_registered and any(totals[k] for k in ("cgst", "sgst", "igst", "cess")):
            raise ApiError(409, "totals_mismatch", "A bill of supply cannot carry GST.")
        doc_type = "TAX_INVOICE" if body.gst_registered else "BILL_OF_SUPPLY"
        series = body.invoice_prefix[:4]  # keeps 'XXXX/26-27/00001' within 16 characters
        version_ids = sorted(
            {ln["tax_rate_version_id"] for ln in lines if ln["tax_rate_version_id"]}
        )
        for attempt in range(3):
            try:
                now = utc_now()
                fy = financial_year(india_date(now), body.fy_start_month)
                number = next_number(db, b, fy, series)
                display = document_number(series, fy, number)
                seller, buyer = body.seller.model_dump(), body.buyer.model_dump()
                digest = canonical_hash(
                    {
                        "b": b,
                        "n": display,
                        "seller": seller,
                        "buyer": buyer,
                        "lines": [{k: v for k, v in ln.items() if k != "rates"} for ln in lines],
                        "grand": body.grand_total,
                    }
                )
                inv = Invoice(
                    business_id=b,
                    order_id=body.order_id,
                    customer_id=body.customer_id,
                    doc_type=doc_type,
                    series=series,
                    fy=fy,
                    number=number,
                    display_number=display,
                    status="ISSUED",
                    issued_at=now,
                    seller=seller,
                    buyer=buyer,
                    place_of_supply_state=body.place_of_supply_state,
                    is_inter_state=body.inter_state,
                    taxable_total=totals["taxable"],
                    cgst_total=totals["cgst"],
                    sgst_total=totals["sgst"],
                    igst_total=totals["igst"],
                    cess_total=totals["cess"],
                    round_off=body.round_off,
                    grand_total=body.grand_total,
                    prices_include_tax=body.prices_include_tax,
                    tax_rate_version_ids=version_ids,
                    content_hash=digest,
                    created_at=now,
                )
                db.add(inv)
                db.flush()
                for pos, ln in enumerate(lines, start=1):
                    cg_rate, sg_rate, ig_rate = ln["rates"]
                    db.add(
                        InvoiceLine(
                            business_id=b,
                            invoice_id=inv.id,
                            position=pos,
                            description=ln["description"],
                            hsn_sac=ln["hsn_sac"],
                            quantity=ln["quantity"],
                            unit=ln["unit"],
                            unit_price=ln["unit_price"],
                            discount=ln["discount"],
                            taxable_value=ln["taxable"],
                            cgst_rate=cg_rate if ln["cgst"] else ZERO,
                            cgst=ln["cgst"],
                            sgst_rate=sg_rate if ln["sgst"] else ZERO,
                            sgst=ln["sgst"],
                            igst_rate=ig_rate if ln["igst"] else ZERO,
                            igst=ln["igst"],
                            cess_rate=ZERO,
                            cess=ln["cess"],
                            line_total=ln["line_total"],
                        )
                    )
                db.commit()
                return {**full(db, inv), "replayed": False}
            except IntegrityError:
                db.rollback()
                existing = db.scalar(
                    select(Invoice).where(
                        Invoice.business_id == b, Invoice.order_id == body.order_id
                    )
                )
                if existing is not None:  # a parallel request issued it
                    return {**full(db, existing), "replayed": True}
                if attempt == 2:
                    raise ApiError(409, "conflict", "Please try again.") from None
        raise AssertionError("unreachable")

    @app.get("/internal/v1/invoices")
    def list_invoices(
        ctx: Ctx,
        db: Db,
        page_args: Annotated[tuple[int, int], paging],
        q: Annotated[str | None, Query(max_length=40)] = None,
    ):
        ctx.require_permission("invoices.read")
        b = business(ctx)
        page, size = page_args
        base = select(Invoice).where(Invoice.business_id == b)
        if q:
            base = base.where(Invoice.display_number.contains(q.strip().upper(), autoescape=True))
        total = db.scalar(select(func.count()).select_from(base.subquery()))
        rows = db.scalars(
            base.order_by(Invoice.issued_at.desc(), Invoice.id)
            .limit(size)
            .offset((page - 1) * size)
        ).all()
        return {
            "items": [invoice_out(i) for i in rows],
            "page": page,
            "page_size": size,
            "total": total,
        }

    def find(db: Session, b: str, **where) -> Invoice:
        query = select(Invoice).where(Invoice.business_id == b)
        for key, value in where.items():
            query = query.where(getattr(Invoice, key) == value)
        inv = db.scalar(query)
        if inv is None:
            raise ApiError(404, "not_found", "Not found.")
        return inv

    @app.get("/internal/v1/invoices/by-order/{order_id}")
    def by_order(order_id: str, ctx: Ctx, db: Db):
        inv = find(db, business(ctx), order_id=order_id)
        may_read(ctx, inv)
        return full(db, inv)

    @app.get("/internal/v1/invoices/{invoice_id}")
    def get_invoice(invoice_id: str, ctx: Ctx, db: Db):
        inv = find(db, business(ctx), id=invoice_id)
        may_read(ctx, inv)
        return full(db, inv)

    @app.get("/internal/v1/credit-notes/{note_id}")
    def get_credit_note(note_id: str, ctx: Ctx, db: Db):
        b = business(ctx)
        note = db.scalar(
            select(CreditNote).where(CreditNote.business_id == b, CreditNote.id == note_id)
        )
        if note is None:
            raise ApiError(404, "not_found", "Not found.")
        may_read(ctx, db.get(Invoice, note.invoice_id))
        lines = db.scalars(
            select(CreditNoteLine)
            .where(CreditNoteLine.credit_note_id == note.id)
            .order_by(CreditNoteLine.id)
        ).all()
        return credit_note_out(note, lines)

    # ---- credit notes -------------------------------------------------------------------------
    @app.post("/internal/v1/credit-notes", status_code=201)
    def credit(body: CreditNoteIn, ctx: Ctx, db: Db):
        """Credit returned quantities of invoiced lines.

        Each component (taxable, CGST, ...) is credited as share(already + qty) - share(already),
        so crediting a line in several returns adds up to exactly the invoiced amount, never a
        paisa more."""
        b = sales_only(ctx)
        inv = db.scalar(
            select(Invoice).where(Invoice.business_id == b, Invoice.order_id == body.order_id)
        )
        if inv is None:
            raise ApiError(404, "invoice_not_found", "This order has no invoice to credit.")
        lines = []
        for ln in body.lines:
            if ln.already + ln.quantity > ln.ordered:
                raise ApiError(409, "over_credit", "Cannot credit more than was invoiced.")
            parts = {}
            for k in ("taxable", "cgst", "sgst", "igst", "cess"):
                value = getattr(ln, k)
                parts[k] = proportional(value, ln.already + ln.quantity, ln.ordered) - proportional(
                    value, ln.already, ln.ordered
                )
            parts["line_total"] = sum(parts.values(), ZERO)
            lines.append(
                {
                    "description": ln.description,
                    "hsn_sac": ln.hsn_sac,
                    "quantity": ln.quantity,
                    **parts,
                }
            )
        totals = {
            k: sum((ln[k] for ln in lines), ZERO)
            for k in ("taxable", "cgst", "sgst", "igst", "cess", "line_total")
        }
        grand = round_rupee(totals["line_total"])
        digest = canonical_hash({"invoice": inv.id, "source": body.source_id, "lines": lines})
        same = db.scalar(
            select(CreditNote).where(
                CreditNote.business_id == b,
                CreditNote.invoice_id == inv.id,
                CreditNote.content_hash == digest,
            )
        )
        if same is not None:  # retry of the same return
            return {**credit_note_out(same), "replayed": True}
        credited = (
            db.scalar(
                select(func.coalesce(func.sum(CreditNote.grand_total), 0)).where(
                    CreditNote.invoice_id == inv.id
                )
            )
            or ZERO
        )
        if Decimal(credited) + grand > inv.grand_total:
            raise ApiError(409, "over_credit", "Credit notes cannot exceed the invoice total.")
        for attempt in range(3):
            try:
                now = utc_now()
                fy = financial_year(india_date(now), body.fy_start_month)
                number = next_number(db, b, fy, "CN")
                display = document_number("CN", fy, number)
                note = CreditNote(
                    business_id=b,
                    invoice_id=inv.id,
                    series="CN",
                    fy=fy,
                    number=number,
                    display_number=display,
                    reason=body.reason,
                    issued_at=now,
                    taxable_total=totals["taxable"],
                    cgst_total=totals["cgst"],
                    sgst_total=totals["sgst"],
                    igst_total=totals["igst"],
                    cess_total=totals["cess"],
                    round_off=grand - totals["line_total"],
                    grand_total=grand,
                    content_hash=digest,
                    created_at=now,
                )
                db.add(note)
                db.flush()
                rows = []
                for ln in lines:
                    rows.append(
                        CreditNoteLine(
                            business_id=b,
                            credit_note_id=note.id,
                            description=ln["description"],
                            hsn_sac=ln["hsn_sac"],
                            quantity=ln["quantity"],
                            taxable_value=ln["taxable"],
                            cgst=ln["cgst"],
                            sgst=ln["sgst"],
                            igst=ln["igst"],
                            cess=ln["cess"],
                            line_total=ln["line_total"],
                        )
                    )
                db.add_all(rows)
                db.commit()
                return {**credit_note_out(note, rows), "replayed": False}
            except IntegrityError:
                db.rollback()
                if attempt == 2:
                    raise ApiError(409, "conflict", "Please try again.") from None
        raise AssertionError("unreachable")
