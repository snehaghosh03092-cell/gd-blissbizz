"""Returns and refunds (docs/04 section 6.3).

    customer requests (within the shop's return window, delivered items only)
      -> REQUESTED --approve--> APPROVED --receive--> RECEIVED --refund--> REFUNDED
                   \\--reject--> REJECTED (order goes back to DELIVERED / RETURNED)

* Only one open return per order at a time; quantities can never exceed what was delivered
  minus what is already being returned.
* receive: goods go back to the warehouse they left from (supply CUSTOMER_RETURN at their sale
  cost); items marked DAMAGED are written off at once.
* refund: billing issues a GST credit note for exactly the returned share of each invoiced line
  (shipping is not refunded), and the refund is recorded (cash / bank transfer / UPI by hand).
  A refund never exceeds what was collected.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Literal

from app.db.models._common import utc_now
from app.db.models.sales import Order, OrderItem, ReturnRequest, ReturnRequestItem, Shipment
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from blissbiz_common.tax import round2, round_rupee
from fastapi import FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales_service.ops import OPEN_RETURN, OrderOps
from sales_service.shop import FRACTIONAL_UNITS, qty_str

ZERO = Decimal(0)
Qty = Annotated[Decimal, Field(gt=0, le=1000, max_digits=14, decimal_places=3)]


class ReturnLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_item_id: str = Field(max_length=36)
    quantity: Qty


class ReturnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=255)
    items: list[ReturnLineIn] = Field(min_length=1, max_length=50)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=255)


class ReceiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    damaged_item_ids: list[str] = Field(
        default_factory=list, max_length=50
    )  # the rest is resellable


class RefundIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["CASH", "BANK_TRANSFER", "UPI"] = "BANK_TRANSFER"
    reference: str | None = Field(default=None, max_length=80)


def install_return_routes(app: FastAPI, *, Ctx, Db, remote, ops: OrderOps) -> None:
    def returned_so_far(
        db: Session,
        order_id: str,
        statuses=("REQUESTED", "APPROVED", "RECEIVED", "REFUNDED"),
        exclude: str | None = None,
    ) -> dict[str, Decimal]:
        query = (
            select(ReturnRequestItem.order_item_id, func.sum(ReturnRequestItem.quantity))
            .join(ReturnRequest, ReturnRequest.id == ReturnRequestItem.return_id)
            .where(ReturnRequest.order_id == order_id, ReturnRequest.status.in_(statuses))
            .group_by(ReturnRequestItem.order_item_id)
        )
        if exclude:
            query = query.where(ReturnRequest.id != exclude)
        return {k: Decimal(v) for k, v in db.execute(query)}

    def load_return(db: Session, b: str, return_id: str) -> tuple[ReturnRequest, Order]:
        r = db.scalar(
            select(ReturnRequest)
            .where(ReturnRequest.business_id == b, ReturnRequest.id == return_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if r is None:
            raise ApiError(404, "not_found", "Not found.")
        return r, ops.load(db, b, r.order_id, lock=True)

    def after_close(db: Session, o: Order, r: ReturnRequest) -> str:
        """Order status once a return is closed: RETURNED if anything came back, else DELIVERED."""
        back = db.scalar(
            select(func.count())
            .select_from(ReturnRequest)
            .where(
                ReturnRequest.order_id == o.id,
                ReturnRequest.status.in_(("RECEIVED", "REFUNDED")),
                ReturnRequest.id != r.id,
            )
        )
        return "RETURNED" if back else "DELIVERED"

    # ---- customer ------------------------------------------------------------------------------
    @app.post("/internal/v1/my/orders/{order_id}/returns", status_code=201)
    def request_return(order_id: str, body: ReturnIn, ctx: Ctx, db: Db):
        b = ops.business(ctx)
        if ctx.actor_type != "CUSTOMER" or not ctx.actor_id:
            raise ApiError(401, "authentication_required", "Please sign in to continue.")
        policy = remote.call("store", "GET", "/internal/v1/policies", remote.service_ctx(ctx))
        o = ops.load(db, b, order_id, lock=True, customer=ctx.actor_id)
        if db.scalar(
            select(ReturnRequest.id).where(
                ReturnRequest.order_id == o.id, ReturnRequest.status.in_(OPEN_RETURN)
            )
        ):
            raise ApiError(409, "return_open", "A return for this order is already in progress.")
        if o.order_status not in ("DELIVERED", "RETURNED"):
            raise ApiError(409, "not_returnable", "Only delivered orders can be returned.")
        window = policy.get("return_window_days")
        if not window:
            raise ApiError(409, "returns_not_accepted", "This shop does not accept returns.")
        delivered = (
            db.scalar(select(func.max(Shipment.delivered_at)).where(Shipment.order_id == o.id))
            or o.updated_at
        )
        if utc_now() - delivered > timedelta(days=window):
            raise ApiError(
                409,
                "return_window_passed",
                f"Returns are accepted within {window} days of delivery.",
            )
        items = {i.id: i for i in db.scalars(select(OrderItem).where(OrderItem.order_id == o.id))}
        taken = returned_so_far(db, o.id)
        ids = [ln.order_item_id for ln in body.items]
        if len(set(ids)) != len(ids):
            raise ApiError(422, "validation_error", "Each item may appear once.")
        problems = {}
        for ln in body.items:
            item = items.get(ln.order_item_id)
            if item is None:
                raise ApiError(
                    422,
                    "validation_error",
                    "That item is not in this order.",
                    {ln.order_item_id: "Not in this order."},
                )
            left = item.quantity - taken.get(item.id, ZERO)
            if (
                item.stock_unit not in FRACTIONAL_UNITS
                and ln.quantity != ln.quantity.to_integral_value()
            ):
                problems[item.id] = "Whole numbers only."
            elif ln.quantity > left:
                problems[item.id] = f"At most {qty_str(left)} can be returned."
        if problems:
            raise ApiError(409, "too_many", "Some quantities cannot be returned.", problems)
        now = utc_now()
        r = ReturnRequest(
            business_id=b,
            order_id=o.id,
            status="REQUESTED",
            reason=body.reason,
            requested_by=ctx.actor_id,
            created_at=now,
            updated_at=now,
        )
        db.add(r)
        db.flush()
        for ln in body.items:
            db.add(
                ReturnRequestItem(
                    return_id=r.id,
                    business_id=b,
                    order_item_id=ln.order_item_id,
                    quantity=ln.quantity,
                    item_condition="RESELLABLE",
                )
            )
        ops.history(db, o, ctx, "ORDER", o.order_status, "RETURN_REQUESTED", body.reason, now)
        o.order_status, o.updated_at = "RETURN_REQUESTED", now
        db.commit()
        return ops.detail(db, o, owner=False)

    # ---- owner -----------------------------------------------------------------------------------
    @app.get("/internal/v1/returns")
    def list_returns(ctx: Ctx, db: Db, status: Annotated[str | None, Query(max_length=12)] = None):
        ctx.require_permission("sales.read")
        b = ops.business(ctx)
        query = (
            select(ReturnRequest, Order.order_number)
            .join(Order, Order.id == ReturnRequest.order_id)
            .where(ReturnRequest.business_id == b)
        )
        if status:
            query = query.where(ReturnRequest.status == status.upper())
        rows = db.execute(query.order_by(ReturnRequest.created_at.desc()).limit(200)).all()
        return {
            "items": [
                {
                    "id": r.id,
                    "order_id": r.order_id,
                    "order_number": number,
                    "status": r.status,
                    "reason": r.reason,
                    "created_at": r.created_at.isoformat(),
                }
                for r, number in rows
            ]
        }

    def decide(
        ctx: InternalContext, db: Session, return_id: str, to: str, note: str | None
    ) -> dict:
        ctx.require_permission("sales.returns")
        r, o = load_return(db, ops.business(ctx), return_id)
        if r.status == to:
            return ops.detail(db, o, owner=True)
        allowed = {"APPROVED": ("REQUESTED",), "REJECTED": ("REQUESTED", "APPROVED")}[to]
        if r.status not in allowed:
            raise ApiError(
                409, "invalid_transition", f"A {r.status.lower()} return cannot be {to.lower()}."
            )
        now = utc_now()
        r.status, r.decided_by, r.updated_at = to, ctx.actor_id, now
        if to == "REJECTED":
            back = after_close(db, o, r)
            ops.history(db, o, ctx, "ORDER", o.order_status, back, note or "Return rejected", now)
            o.order_status, o.updated_at = back, now
        db.commit()
        return ops.detail(db, o, owner=True)

    @app.post("/internal/v1/returns/{return_id}/approve")
    def approve(return_id: str, body: DecisionIn, ctx: Ctx, db: Db):
        return decide(ctx, db, return_id, "APPROVED", body.note)

    @app.post("/internal/v1/returns/{return_id}/reject")
    def reject(return_id: str, body: DecisionIn, ctx: Ctx, db: Db):
        return decide(ctx, db, return_id, "REJECTED", body.note)

    @app.post("/internal/v1/returns/{return_id}/receive")
    def receive(return_id: str, body: ReceiveIn, ctx: Ctx, db: Db):
        ctx.require_permission("sales.returns")
        b = ops.business(ctx)
        r, o = load_return(db, b, return_id)
        if r.status in ("RECEIVED", "REFUNDED"):
            return ops.detail(db, o, owner=True)
        if r.status != "APPROVED":
            raise ApiError(
                409, "invalid_transition", "Approve the return before receiving the goods."
            )
        items = list(
            db.scalars(select(ReturnRequestItem).where(ReturnRequestItem.return_id == r.id))
        )
        unknown = set(body.damaged_item_ids) - {i.id for i in items}
        if unknown:
            raise ApiError(
                422,
                "validation_error",
                "Unknown returned item.",
                dict.fromkeys(unknown, "Unknown."),
            )
        variants = {
            i.id: i.variant_id
            for i in db.scalars(select(OrderItem).where(OrderItem.order_id == o.id))
        }
        lines = [
            {
                "variant_id": variants[i.order_item_id],
                "quantity": str(i.quantity),
                "condition": "DAMAGED" if i.id in body.damaged_item_ids else "RESELLABLE",
                "key": f"{r.id}:{i.id}",
            }
            for i in items
        ]
        remote.call(
            "supply",
            "POST",
            f"/internal/v1/returns/{o.id}/receive",
            remote.service_ctx(ctx),
            json_body={"return_id": r.id, "lines": lines},
        )
        r, o = load_return(db, b, return_id)
        now = utc_now()
        for i in db.scalars(select(ReturnRequestItem).where(ReturnRequestItem.return_id == r.id)):
            i.item_condition = "DAMAGED" if i.id in body.damaged_item_ids else "RESELLABLE"
        r.status, r.updated_at = "RECEIVED", now
        ops.history(db, o, ctx, "ORDER", o.order_status, "RETURNED", "Returned goods received", now)
        o.order_status, o.updated_at = "RETURNED", now
        db.commit()
        return ops.detail(db, o, owner=True)

    @app.post("/internal/v1/returns/{return_id}/refund")
    def refund(return_id: str, body: RefundIn, ctx: Ctx, db: Db):
        ctx.require_permission("sales.refund")
        b = ops.business(ctx)
        r, o = load_return(db, b, return_id)
        if r.status == "REFUNDED":
            return ops.detail(db, o, owner=True, ctx=ctx)
        if r.status != "RECEIVED":
            raise ApiError(
                409, "invalid_transition", "Receive the returned goods before refunding."
            )
        if ops.captured(db, o) is None:
            raise ApiError(409, "nothing_to_refund", "No payment was collected for this order.")
        order_items = {
            i.id: i for i in db.scalars(select(OrderItem).where(OrderItem.order_id == o.id))
        }
        before = returned_so_far(db, o.id, ("REFUNDED",), exclude=r.id)
        lines = []
        for i in db.scalars(
            select(ReturnRequestItem)
            .where(ReturnRequestItem.return_id == r.id)
            .order_by(ReturnRequestItem.id)
        ):
            oi = order_items[i.order_item_id]
            lines.append(
                {
                    "description": oi.product_name[:300],
                    "hsn_sac": oi.hsn_sac,
                    "ordered": str(oi.quantity),
                    "already": str(before.get(oi.id, ZERO)),
                    "quantity": str(i.quantity),
                    "taxable": f"{oi.taxable_value:.2f}",
                    "cgst": f"{oi.cgst:.2f}",
                    "sgst": f"{oi.sgst:.2f}",
                    "igst": f"{oi.igst:.2f}",
                    "cess": f"{oi.cess:.2f}",
                    "_line_total": oi.line_total,
                }
            )
        profile = remote.profile(ctx)
        has_invoice = (
            remote.response(
                "billing", "GET", f"/internal/v1/invoices/by-order/{o.id}", remote.service_ctx(ctx)
            ).status_code
            == 200
        )
        if not has_invoice and o.order_status in (
            "SHIPPED",
            "DELIVERED",
            "RETURN_REQUESTED",
            "RETURNED",
        ):
            ops.invoice_for(
                db, ctx, o
            )  # issue the missing invoice first, so the credit note has a parent
            has_invoice = True
        if has_invoice:
            note = remote.call(
                "billing",
                "POST",
                "/internal/v1/credit-notes",
                remote.service_ctx(ctx),
                json_body={
                    "order_id": o.id,
                    "source_id": f"return:{r.id}",
                    "reason": f"Goods returned: {r.reason}"[:255],
                    "fy_start_month": profile.get("fy_start_month") or 4,
                    "lines": [
                        {k: v for k, v in ln.items() if not k.startswith("_")} for ln in lines
                    ],
                },
            )
            amount = Decimal(note["grand_total"])
        else:  # defensive: an order with no invoice is credited by the same proportional rule
            amount = round_rupee(
                sum(
                    (
                        round2(
                            ln["_line_total"]
                            * (Decimal(ln["already"]) + Decimal(ln["quantity"]))
                            / Decimal(ln["ordered"])
                        )
                        - round2(
                            ln["_line_total"] * Decimal(ln["already"]) / Decimal(ln["ordered"])
                        )
                        for ln in lines
                    ),
                    ZERO,
                )
            )
        r, o = load_return(db, b, return_id)
        now = utc_now()
        reference = f"{body.method}{': ' + body.reference if body.reference else ''}"[:80]
        ops.record_refund(
            db,
            ctx,
            o,
            amount,
            key=f"return:{r.id}",
            reason=f"Return refund ({body.method})",
            reference=reference,
            now=now,
        )
        r.status, r.updated_at = "REFUNDED", now
        db.commit()
        return ops.detail(db, o, owner=True, ctx=ctx)
