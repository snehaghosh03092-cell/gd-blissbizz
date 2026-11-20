"""Purchasing (docs/04 section 8): suppliers, purchase orders, goods receipts, supplier invoices
and supplier payments. Owned by supply because receiving goods is a stock movement.

* A purchase order is edited only while DRAFT; approving it needs ``purchasing.approve``.
* Receiving is limited to what is still outstanding on each line; the order row is locked while
  receiving, so two people receiving at once can never take in more than was ordered. Each
  receipt posts RECEIPT ledger rows at the order's unit cost and updates the average cost.
* A supplier invoice keeps its original document untouched (SHA-256 recorded), is checked for
  arithmetic, keeps a history of every correction, and creates a payable only once APPROVED.
"""

import base64
import binascii
import hashlib
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from app.core.gstin import validate_gstin
from app.db.models import utc_now
from app.db.models.supply import (
    Document,
    GoodsReceipt,
    GoodsReceiptItem,
    InventoryBalance,
    InventoryTransaction,
    PurchaseInvoice,
    PurchaseOrder,
    PurchaseOrderItem,
    Supplier,
    SupplierPayment,
    Warehouse,
)
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from blissbiz_common.paging import page_query
from fastapi import Depends, FastAPI, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from supply_service.stock_ops import avg_after_inbound, q3

ZERO = Decimal(0)
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MIME_BY_MAGIC = (
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
)
Qty = Annotated[Decimal, Field(gt=0, le=1_000_000, max_digits=14, decimal_places=3)]
Cost = Annotated[Decimal, Field(ge=0, le=10_000_000, max_digits=14, decimal_places=4)]
Money = Annotated[Decimal, Field(ge=0, le=100_000_000, max_digits=12, decimal_places=2)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]


def money(value) -> str:
    return f"{value:.2f}"


class SupplierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    contact_name: str | None = Field(default=None, max_length=150)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    gstin: str | None = Field(default=None, max_length=15)
    address: str | None = Field(default=None, max_length=1000)
    lead_time_days: int = Field(default=7, ge=0, le=365)
    payment_terms_days: int = Field(default=0, ge=0, le=365)

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, value):
        if not value:
            return None
        result = validate_gstin(value)
        if not result.ok:
            raise ValueError("This GSTIN is not valid.")
        return result.normalized


class SupplierPatch(SupplierIn):
    name: Name | None = None  # type: ignore[assignment]
    lead_time_days: int | None = Field(default=None, ge=0, le=365)  # type: ignore[assignment]
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)  # type: ignore[assignment]
    archived: bool | None = None


class POLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant_id: str = Field(min_length=1, max_length=36)
    quantity: Qty
    unit_cost: Cost


class POIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str = Field(max_length=36)
    expected_date: date | None = None
    lines: list[POLine] = Field(min_length=1, max_length=200)


class ReceiptLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    po_item_id: str = Field(max_length=36)
    quantity: Qty


class ReceiptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,64}$")
    warehouse_id: str = Field(max_length=36)
    lines: list[ReceiptLine] = Field(min_length=1, max_length=200)


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=4, max_length=MAX_DOCUMENT_BYTES * 4 // 3 + 8)


class PurchaseInvoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(max_length=36)
    supplier_id: str | None = Field(default=None, max_length=36)
    supplier_invoice_number: str | None = Field(default=None, max_length=60)
    invoice_date: date | None = None
    due_date: date | None = None
    taxable_total: Money = ZERO
    tax_total: Money = ZERO
    grand_total: Money = ZERO
    linked_po_id: str | None = Field(default=None, max_length=36)


class PurchaseInvoicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str | None = Field(default=None, max_length=36)
    supplier_invoice_number: str | None = Field(default=None, max_length=60)
    invoice_date: date | None = None
    due_date: date | None = None
    taxable_total: Money | None = None
    tax_total: Money | None = None
    grand_total: Money | None = None
    linked_po_id: str | None = Field(default=None, max_length=36)


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Annotated[Decimal, Field(gt=0, le=100_000_000, max_digits=12, decimal_places=2)]
    reference: str | None = Field(default=None, max_length=80)


def supplier_out(s: Supplier) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "contact_name": s.contact_name,
        "email": s.email,
        "phone": s.phone,
        "gstin": s.gstin,
        "address": s.address,
        "lead_time_days": s.lead_time_days,
        "payment_terms_days": s.payment_terms_days,
        "archived": s.archived_at is not None,
    }


def install_purchasing_routes(app: FastAPI, *, Ctx, Db, catalog_facts, upload_dir: Path) -> None:
    paging = Depends(page_query())

    def business(ctx: InternalContext) -> str:
        if not ctx.business_id:
            raise ApiError(404, "not_found", "Not found.")
        return ctx.business_id

    def one(db: Session, model, b: str, row_id: str, *, lock=False):
        query = select(model).where(model.business_id == b, model.id == row_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        row = db.scalar(query)
        if row is None:
            raise ApiError(404, "not_found", "Not found.")
        return row

    def active_supplier(db: Session, b: str, supplier_id: str) -> Supplier:
        s = db.scalar(select(Supplier).where(Supplier.business_id == b, Supplier.id == supplier_id))
        if s is None or s.archived_at is not None:
            raise ApiError(
                422, "validation_error", "Unknown supplier.", {"supplier_id": "Unknown supplier."}
            )
        return s

    # ---- suppliers ---------------------------------------------------------------------------
    @app.get("/internal/v1/suppliers")
    def suppliers(ctx: Ctx, db: Db, include_archived: bool = False):
        ctx.require_permission("purchasing.read")
        query = select(Supplier).where(Supplier.business_id == business(ctx))
        if not include_archived:
            query = query.where(Supplier.archived_at.is_(None))
        return {"items": [supplier_out(s) for s in db.scalars(query.order_by(Supplier.name))]}

    @app.post("/internal/v1/suppliers", status_code=201)
    def create_supplier(body: SupplierIn, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.write")
        b = business(ctx)
        s = Supplier(business_id=b, **body.model_dump())
        db.add(s)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ApiError(
                409, "name_taken", "A supplier with this name exists.", {"name": "Already used."}
            ) from None
        return supplier_out(s)

    @app.patch("/internal/v1/suppliers/{supplier_id}")
    def update_supplier(supplier_id: str, body: SupplierPatch, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.write")
        s = one(db, Supplier, business(ctx), supplier_id)
        changes = body.model_dump(exclude_unset=True)
        archived = changes.pop("archived", None)
        for key, value in changes.items():
            if key in ("name", "lead_time_days", "payment_terms_days") and value is None:
                continue
            setattr(s, key, value)
        if archived is not None:
            s.archived_at = utc_now() if archived else None
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ApiError(
                409, "name_taken", "A supplier with this name exists.", {"name": "Already used."}
            ) from None
        return supplier_out(s)

    # ---- purchase orders ------------------------------------------------------------------------
    def po_out(db: Session, po: PurchaseOrder, detail=False) -> dict:
        supplier = db.get(Supplier, po.supplier_id)
        out = {
            "id": po.id,
            "number": po.number,
            "status": po.status,
            "supplier_id": po.supplier_id,
            "supplier": supplier.name if supplier else None,
            "total": money(po.total),
            "expected_date": po.expected_date.isoformat() if po.expected_date else None,
            "created_at": po.created_at.isoformat(),
            "overdue": bool(
                po.expected_date
                and po.expected_date < utc_now().date()
                and po.status in ("APPROVED", "PARTIALLY_RECEIVED")
            ),
        }
        if detail:
            items = db.scalars(
                select(PurchaseOrderItem)
                .where(PurchaseOrderItem.purchase_order_id == po.id)
                .order_by(PurchaseOrderItem.id)
            ).all()
            out["items"] = [
                {
                    "id": i.id,
                    "variant_id": i.variant_id,
                    "quantity": q3(i.quantity),
                    "unit_cost": f"{i.unit_cost:.4f}",
                    "received_quantity": q3(i.received_quantity),
                    "outstanding": q3(i.quantity - i.received_quantity),
                }
                for i in items
            ]
            receipts = db.scalars(
                select(GoodsReceipt)
                .where(GoodsReceipt.purchase_order_id == po.id)
                .order_by(GoodsReceipt.received_at)
            ).all()
            out["receipts"] = [
                {
                    "id": r.id,
                    "number": r.number,
                    "received_at": r.received_at.isoformat(),
                    "warehouse_id": r.warehouse_id,
                }
                for r in receipts
            ]
        return out

    def next_number(db: Session, model, b: str, prefix: str) -> str:
        last = db.scalars(
            select(model.number).where(model.business_id == b, model.number.like(f"{prefix}-%"))
        ).all()
        numbers = [int(n.split("-")[-1]) for n in last if n.split("-")[-1].isdigit()]
        return f"{prefix}-{(max(numbers) if numbers else 0) + 1:05d}"

    def write_lines(
        db: Session, ctx: InternalContext, po: PurchaseOrder, lines: list[POLine]
    ) -> None:
        ids = [ln.variant_id for ln in lines]
        if len(set(ids)) != len(ids):
            raise ApiError(422, "validation_error", "Each item may appear once per purchase order.")
        for old in db.scalars(
            select(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == po.id)
        ):
            db.delete(old)
        db.flush()
        total = ZERO
        for ln in lines:
            db.add(
                PurchaseOrderItem(
                    purchase_order_id=po.id,
                    business_id=po.business_id,
                    variant_id=ln.variant_id,
                    quantity=ln.quantity,
                    unit_cost=ln.unit_cost,
                )
            )
            total += (ln.quantity * ln.unit_cost).quantize(Decimal("0.01"))
        po.total = total

    @app.get("/internal/v1/purchase-orders")
    def list_pos(
        ctx: Ctx,
        db: Db,
        page_args: Annotated[tuple[int, int], paging],
        status: Annotated[str | None, Query(max_length=20)] = None,
        supplier_id: Annotated[str | None, Query(max_length=36)] = None,
    ):
        ctx.require_permission("purchasing.read")
        page, size = page_args
        base = select(PurchaseOrder).where(PurchaseOrder.business_id == business(ctx))
        if status:
            base = base.where(PurchaseOrder.status == status.upper())
        if supplier_id:
            base = base.where(PurchaseOrder.supplier_id == supplier_id)
        total = db.scalar(select(func.count()).select_from(base.subquery()))
        rows = db.scalars(
            base.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id)
            .limit(size)
            .offset((page - 1) * size)
        ).all()
        return {
            "items": [po_out(db, p) for p in rows],
            "page": page,
            "page_size": size,
            "total": total,
        }

    @app.get("/internal/v1/purchase-orders/{po_id}")
    def get_po(po_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.read")
        return po_out(db, one(db, PurchaseOrder, business(ctx), po_id), detail=True)

    @app.post("/internal/v1/purchase-orders", status_code=201)
    def create_po(body: POIn, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.write")
        b = business(ctx)
        catalog_facts(ctx, [ln.variant_id for ln in body.lines])
        active_supplier(db, b, body.supplier_id)
        for attempt in range(3):
            po = PurchaseOrder(
                business_id=b,
                supplier_id=body.supplier_id,
                number=next_number(db, PurchaseOrder, b, "PO"),
                status="DRAFT",
                expected_date=body.expected_date,
                created_by=ctx.actor_id,
            )
            db.add(po)
            try:
                db.flush()
                write_lines(db, ctx, po, body.lines)
                db.commit()
                return po_out(db, po, detail=True)
            except IntegrityError:
                db.rollback()
                if attempt == 2:
                    raise ApiError(409, "conflict", "Please try again.") from None
        raise AssertionError("unreachable")

    @app.put("/internal/v1/purchase-orders/{po_id}")
    def edit_po(po_id: str, body: POIn, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.write")
        b = business(ctx)
        catalog_facts(ctx, [ln.variant_id for ln in body.lines])
        po = one(db, PurchaseOrder, b, po_id, lock=True)
        if po.status != "DRAFT":
            raise ApiError(409, "invalid_transition", "Only draft purchase orders can be edited.")
        active_supplier(db, b, body.supplier_id)
        po.supplier_id, po.expected_date = body.supplier_id, body.expected_date
        write_lines(db, ctx, po, body.lines)
        db.commit()
        return po_out(db, po, detail=True)

    @app.post("/internal/v1/purchase-orders/{po_id}/approve")
    def approve_po(po_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.approve")
        po = one(db, PurchaseOrder, business(ctx), po_id, lock=True)
        if po.status == "APPROVED":
            return po_out(db, po, detail=True)
        if po.status != "DRAFT":
            raise ApiError(
                409,
                "invalid_transition",
                f"A {po.status.lower()} purchase order cannot be approved.",
            )
        po.status = "APPROVED"
        db.commit()
        return po_out(db, po, detail=True)

    @app.post("/internal/v1/purchase-orders/{po_id}/cancel")
    def cancel_po(po_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.write")
        po = one(db, PurchaseOrder, business(ctx), po_id, lock=True)
        if po.status == "CANCELLED":
            return po_out(db, po, detail=True)
        if po.status not in ("DRAFT", "APPROVED"):
            raise ApiError(
                409, "invalid_transition", "Goods were already received on this purchase order."
            )
        po.status = "CANCELLED"
        db.commit()
        return po_out(db, po, detail=True)

    @app.post("/internal/v1/purchase-orders/{po_id}/receipts", status_code=201)
    def receive(po_id: str, body: ReceiptIn, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.receive")
        b = business(ctx)
        same = db.scalar(
            select(GoodsReceipt).where(
                GoodsReceipt.business_id == b, GoodsReceipt.idempotency_key == body.idempotency_key
            )
        )
        if same is not None:
            return {
                "receipt_id": same.id,
                "number": same.number,
                "replayed": True,
                "purchase_order": po_out(db, one(db, PurchaseOrder, b, po_id), detail=True),
            }
        po = one(db, PurchaseOrder, b, po_id, lock=True)  # serialises concurrent receipts
        if po.status not in ("APPROVED", "PARTIALLY_RECEIVED"):
            raise ApiError(
                409, "invalid_transition", "Only approved purchase orders can be received."
            )
        wh = db.scalar(
            select(Warehouse).where(
                Warehouse.business_id == b,
                Warehouse.id == body.warehouse_id,
                Warehouse.archived_at.is_(None),
            )
        )
        if wh is None:
            raise ApiError(
                422,
                "validation_error",
                "Unknown warehouse.",
                {"warehouse_id": "Unknown warehouse."},
            )
        items = {
            i.id: i
            for i in db.scalars(
                select(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == po.id)
            )
        }
        ids = [ln.po_item_id for ln in body.lines]
        if len(set(ids)) != len(ids):
            raise ApiError(422, "validation_error", "Each line may appear once.")
        over = {}
        for ln in body.lines:
            item = items.get(ln.po_item_id)
            if item is None:
                raise ApiError(
                    422,
                    "validation_error",
                    "Unknown purchase order line.",
                    {ln.po_item_id: "Unknown line."},
                )
            if ln.quantity > item.quantity - item.received_quantity:
                over[ln.po_item_id] = q3(item.quantity - item.received_quantity)
        if over:
            raise ApiError(409, "over_receipt", "That is more than is still outstanding.", over)
        now = utc_now()
        receipt = GoodsReceipt(
            business_id=b,
            purchase_order_id=po.id,
            supplier_id=po.supplier_id,
            warehouse_id=wh.id,
            number=next_number(db, GoodsReceipt, b, "GRN"),
            received_at=now,
            status="POSTED",
            idempotency_key=body.idempotency_key,
            created_by=ctx.actor_id,
        )
        db.add(receipt)
        db.flush()
        for ln in sorted(body.lines, key=lambda x: items[x.po_item_id].variant_id):
            item = items[ln.po_item_id]
            db.add(
                GoodsReceiptItem(
                    receipt_id=receipt.id,
                    business_id=b,
                    variant_id=item.variant_id,
                    quantity=ln.quantity,
                    unit_cost=item.unit_cost,
                )
            )
            bal = db.scalar(
                select(InventoryBalance)
                .where(
                    InventoryBalance.business_id == b,
                    InventoryBalance.variant_id == item.variant_id,
                    InventoryBalance.warehouse_id == wh.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if bal is None:
                bal = InventoryBalance(
                    business_id=b,
                    variant_id=item.variant_id,
                    warehouse_id=wh.id,
                    on_hand=ZERO,
                    reserved=ZERO,
                    avg_cost=ZERO,
                    version=0,
                )
                db.add(bal)
                db.flush()
            bal.avg_cost = avg_after_inbound(bal, ln.quantity, item.unit_cost)
            bal.on_hand += ln.quantity
            bal.version += 1
            db.add(
                InventoryTransaction(
                    business_id=b,
                    variant_id=item.variant_id,
                    warehouse_id=wh.id,
                    txn_type="RECEIPT",
                    quantity_delta=ln.quantity,
                    unit_cost=item.unit_cost,
                    reason_code="PURCHASE_RECEIPT",
                    source_type="GOODS_RECEIPT",
                    source_id=receipt.id,
                    idempotency_key=f"grn:{receipt.id}:{item.id}",
                    actor_type=ctx.actor_type,
                    actor_id=ctx.actor_id,
                    created_at=now,
                )
            )
            item.received_quantity += ln.quantity
        complete = all(i.received_quantity >= i.quantity for i in items.values())
        po.status = "RECEIVED" if complete else "PARTIALLY_RECEIVED"
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            same = db.scalar(
                select(GoodsReceipt).where(
                    GoodsReceipt.business_id == b,
                    GoodsReceipt.idempotency_key == body.idempotency_key,
                )
            )
            if same is None:
                raise ApiError(409, "conflict", "Please try again.") from None
            receipt = same
        return {
            "receipt_id": receipt.id,
            "number": receipt.number,
            "replayed": False,
            "purchase_order": po_out(db, one(db, PurchaseOrder, b, po_id), detail=True),
        }

    # ---- documents (original files are stored untouched) ----------------------------------------
    @app.post("/internal/v1/documents", status_code=201)
    def upload(body: DocumentIn, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.write")
        b = business(ctx)
        try:
            raw = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(422, "validation_error", "The file could not be read.") from None
        if not raw:
            raise ApiError(422, "validation_error", "The file is empty.")
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ApiError(413, "file_too_large", "Files can be at most 10 MB.")
        kind = next(
            ((mime, ext) for magic, mime, ext in MIME_BY_MAGIC if raw.startswith(magic)), None
        )
        if kind is None:  # judged by content, never by the file name
            raise ApiError(415, "unsupported_file", "Upload a PDF, PNG or JPEG file.")
        mime, ext = kind
        digest = hashlib.sha256(raw).hexdigest()
        doc = Document(
            business_id=b,
            kind="PURCHASE_INVOICE",
            storage_path="",
            mime=mime,
            original_name=Path(body.filename).name[:255] or f"document.{ext}",
            byte_size=len(raw),
            sha256=digest,
            uploaded_by=ctx.actor_id,
        )
        db.add(doc)
        db.flush()
        folder = upload_dir / b
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{doc.id}.{ext}"  # generated name: no path tricks from the client
        path.write_bytes(raw)
        doc.storage_path = str(path)
        db.commit()
        return {
            "id": doc.id,
            "name": doc.original_name,
            "mime": mime,
            "bytes": len(raw),
            "sha256": digest,
        }

    @app.get("/internal/v1/documents/{document_id}/content")
    def download(document_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.read")
        doc = one(db, Document, business(ctx), document_id)
        path = Path(doc.storage_path)
        if not path.is_file():
            raise ApiError(404, "not_found", "The file is missing.")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != doc.sha256:
            raise ApiError(
                409, "document_changed", "The stored file does not match its fingerprint."
            )
        safe = "".join(c for c in doc.original_name if c.isalnum() or c in "._- ") or "document"
        return Response(
            raw,
            media_type=doc.mime,
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )

    # ---- supplier invoices -----------------------------------------------------------------------
    def pi_out(pi: PurchaseInvoice, db: Session) -> dict:
        supplier = db.get(Supplier, pi.supplier_id) if pi.supplier_id else None
        return {
            "id": pi.id,
            "status": pi.status,
            "document_id": pi.document_id,
            "supplier_id": pi.supplier_id,
            "supplier": supplier.name if supplier else None,
            "supplier_invoice_number": pi.supplier_invoice_number,
            "invoice_date": pi.invoice_date.isoformat() if pi.invoice_date else None,
            "due_date": pi.due_date.isoformat() if pi.due_date else None,
            "taxable_total": money(pi.taxable_total),
            "tax_total": money(pi.tax_total),
            "grand_total": money(pi.grand_total),
            "linked_po_id": pi.linked_po_id,
            "approved_at": pi.approved_at.isoformat() if pi.approved_at else None,
            "corrections": pi.correction_history or [],
            "problems": problems_of(pi),
        }

    def problems_of(pi: PurchaseInvoice) -> dict:
        """What still blocks approval (empty = ready)."""
        out = {}
        if not pi.supplier_id:
            out["supplier_id"] = "Choose the supplier."
        if not pi.supplier_invoice_number:
            out["supplier_invoice_number"] = "Enter the supplier's invoice number."
        if not pi.invoice_date:
            out["invoice_date"] = "Enter the invoice date."
        if pi.grand_total <= 0:
            out["grand_total"] = "Enter the invoice total."
        elif pi.taxable_total + pi.tax_total != pi.grand_total:
            out["grand_total"] = (
                f"Taxable {money(pi.taxable_total)} + tax {money(pi.tax_total)} "
                f"does not equal the total {money(pi.grand_total)}."
            )
        if pi.due_date and pi.invoice_date and pi.due_date < pi.invoice_date:
            out["due_date"] = "The due date is before the invoice date."
        return out

    def check_refs(
        db: Session, b: str, supplier_id, linked_po_id, document_id=None, pi_id=None, number=None
    ) -> None:
        if supplier_id:
            active_supplier(db, b, supplier_id)
        if linked_po_id:
            po = one(db, PurchaseOrder, b, linked_po_id)
            if supplier_id and po.supplier_id != supplier_id:
                raise ApiError(
                    422,
                    "validation_error",
                    "That purchase order is from another supplier.",
                    {"linked_po_id": "Different supplier."},
                )
        if document_id:
            one(db, Document, b, document_id)
        if supplier_id and number:
            dup = db.scalar(
                select(PurchaseInvoice.id).where(
                    PurchaseInvoice.business_id == b,
                    PurchaseInvoice.supplier_id == supplier_id,
                    PurchaseInvoice.supplier_invoice_number == number,
                    PurchaseInvoice.status != "REJECTED",
                    *([PurchaseInvoice.id != pi_id] if pi_id else []),
                )
            )
            if dup:
                raise ApiError(
                    409,
                    "duplicate_invoice",
                    "This supplier invoice is already recorded.",
                    {"supplier_invoice_number": "Already recorded."},
                )

    @app.get("/internal/v1/purchase-invoices")
    def list_pis(ctx: Ctx, db: Db, status: Annotated[str | None, Query(max_length=14)] = None):
        ctx.require_permission("purchasing.read")
        query = select(PurchaseInvoice).where(PurchaseInvoice.business_id == business(ctx))
        if status:
            query = query.where(PurchaseInvoice.status == status.upper())
        rows = db.scalars(query.order_by(PurchaseInvoice.created_at.desc()).limit(200)).all()
        return {"items": [pi_out(p, db) for p in rows]}

    @app.get("/internal/v1/purchase-invoices/{pi_id}")
    def get_pi(pi_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.read")
        return pi_out(one(db, PurchaseInvoice, business(ctx), pi_id), db)

    @app.post("/internal/v1/purchase-invoices", status_code=201)
    def create_pi(body: PurchaseInvoiceIn, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.write")
        b = business(ctx)
        check_refs(
            db,
            b,
            body.supplier_id,
            body.linked_po_id,
            body.document_id,
            None,
            body.supplier_invoice_number,
        )
        pi = PurchaseInvoice(
            business_id=b, status="DRAFT", correction_history=[], **body.model_dump()
        )
        db.add(pi)
        db.commit()
        return pi_out(pi, db)

    @app.patch("/internal/v1/purchase-invoices/{pi_id}")
    def edit_pi(pi_id: str, body: PurchaseInvoicePatch, ctx: Ctx, db: Db):
        """Corrections are kept: every change is appended to the invoice's history."""
        ctx.require_permission("purchasing.write")
        b = business(ctx)
        pi = one(db, PurchaseInvoice, b, pi_id, lock=True)
        if pi.status not in ("DRAFT", "NEEDS_REVIEW"):
            raise ApiError(
                409, "invalid_transition", "Approved or rejected invoices cannot be edited."
            )
        changes = body.model_dump(exclude_unset=True)
        merged_supplier = changes.get("supplier_id", pi.supplier_id)
        check_refs(
            db,
            b,
            merged_supplier,
            changes.get("linked_po_id", pi.linked_po_id),
            None,
            pi.id,
            changes.get("supplier_invoice_number", pi.supplier_invoice_number),
        )
        diff = {}
        for key, value in changes.items():
            before = getattr(pi, key)
            if value is None and key in ("taxable_total", "tax_total", "grand_total"):
                continue
            if before != value:
                diff[key] = {
                    "from": str(before) if before is not None else None,
                    "to": str(value) if value is not None else None,
                }
                setattr(pi, key, value)
        if diff:
            pi.correction_history = [
                *(pi.correction_history or []),
                {"at": utc_now().isoformat(), "by": ctx.actor_id, "changes": diff},
            ]
        db.commit()
        return pi_out(pi, db)

    def transition(db: Session, ctx: InternalContext, pi_id: str, to: str) -> PurchaseInvoice:
        pi = one(db, PurchaseInvoice, business(ctx), pi_id, lock=True)
        if pi.status == to:
            return pi
        allowed = {
            "NEEDS_REVIEW": ("DRAFT",),
            "APPROVED": ("DRAFT", "NEEDS_REVIEW"),
            "REJECTED": ("DRAFT", "NEEDS_REVIEW"),
        }[to]
        if pi.status not in allowed:
            raise ApiError(
                409,
                "invalid_transition",
                f"A {pi.status.lower()} invoice cannot become {to.lower()}.",
            )
        if to in ("NEEDS_REVIEW", "APPROVED"):
            problems = problems_of(pi)
            if problems:
                raise ApiError(422, "invoice_incomplete", "Fix these before continuing.", problems)
        pi.status = to
        return pi

    @app.post("/internal/v1/purchase-invoices/{pi_id}/submit")
    def submit_pi(pi_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.write")
        pi = transition(db, ctx, pi_id, "NEEDS_REVIEW")
        db.commit()
        return pi_out(pi, db)

    @app.post("/internal/v1/purchase-invoices/{pi_id}/approve")
    def approve_pi(pi_id: str, ctx: Ctx, db: Db):
        """Approval is a human decision; only now does the invoice become a payable."""
        ctx.require_permission("purchasing.approve")
        b = business(ctx)
        already = db.scalar(
            select(PurchaseInvoice.status).where(
                PurchaseInvoice.business_id == b, PurchaseInvoice.id == pi_id
            )
        )
        pi = transition(db, ctx, pi_id, "APPROVED")
        if already != "APPROVED":
            supplier = db.get(Supplier, pi.supplier_id)
            due = pi.due_date or (pi.invoice_date + timedelta(days=supplier.payment_terms_days))
            pi.approved_by, pi.approved_at = ctx.actor_id, utc_now()
            db.add(
                SupplierPayment(
                    business_id=b,
                    supplier_id=pi.supplier_id,
                    purchase_invoice_id=pi.id,
                    status="DUE",
                    amount=pi.grand_total,
                    amount_paid=ZERO,
                    due_date=due,
                )
            )
        db.commit()
        return pi_out(pi, db)

    @app.post("/internal/v1/purchase-invoices/{pi_id}/reject")
    def reject_pi(pi_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.approve")
        pi = transition(db, ctx, pi_id, "REJECTED")
        db.commit()
        return pi_out(pi, db)

    # ---- supplier payments (recorded by hand; nothing is paid automatically) -------------------
    def payment_out(p: SupplierPayment, db: Session) -> dict:
        supplier = db.get(Supplier, p.supplier_id)
        today = utc_now().date()
        return {
            "id": p.id,
            "supplier_id": p.supplier_id,
            "supplier": supplier.name if supplier else None,
            "purchase_invoice_id": p.purchase_invoice_id,
            "status": p.status,
            "amount": money(p.amount),
            "amount_paid": money(p.amount_paid),
            "balance": money(p.amount - p.amount_paid),
            "due_date": p.due_date.isoformat() if p.due_date else None,
            "overdue": bool(p.due_date and p.due_date < today and p.status != "PAID"),
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            "reference": p.reference,
        }

    @app.get("/internal/v1/supplier-payments")
    def payments(ctx: Ctx, db: Db, status: Annotated[str | None, Query(max_length=8)] = None):
        ctx.require_permission("purchasing.read")
        query = select(SupplierPayment).where(SupplierPayment.business_id == business(ctx))
        if status:
            query = query.where(SupplierPayment.status == status.upper())
        rows = db.scalars(
            query.order_by(SupplierPayment.due_date, SupplierPayment.id).limit(300)
        ).all()
        return {"items": [payment_out(p, db) for p in rows]}

    @app.post("/internal/v1/supplier-payments/{payment_id}/pay")
    def pay(payment_id: str, body: PaymentIn, ctx: Ctx, db: Db):
        ctx.require_permission("purchasing.approve")
        p = one(db, SupplierPayment, business(ctx), payment_id, lock=True)
        balance = p.amount - p.amount_paid
        if body.amount > balance:
            raise ApiError(
                409,
                "overpayment",
                f"Only {money(balance)} is outstanding.",
                {"amount": money(balance)},
            )
        p.amount_paid += body.amount
        p.status = "PAID" if p.amount_paid >= p.amount else "PARTIAL"
        p.paid_at = utc_now()
        if body.reference:
            p.reference = body.reference
        db.commit()
        return payment_out(p, db)
