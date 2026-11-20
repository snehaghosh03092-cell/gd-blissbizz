"""Order lifecycle (docs/04 section 6). Three status axes: order, payment, fulfilment.

    CONFIRMED --pack--> PACKED --ship--> SHIPPED --deliver--> DELIVERED --return--> RETURNED
        \\______________ship_____________/       (invoice)       (COD: cash collected = PAID)
    PENDING_PAYMENT / CONFIRMED / PACKED --cancel--> CANCELLED (stock back; paid -> refunded)

* pack: the stock hold is extended (packed goods are never auto-cancelled by expiry).
* ship: supply turns the holds into SALE ledger rows, the order becomes SHIPPED, then billing
  issues the GST invoice. If billing is down the order still ships and the invoice is issued on
  the next request for it (``invoice: {pending: true}`` until then).
* deliver: COD cash is recorded as a captured payment.
* cancel: held stock is released; a paid order is restocked and refunded in full.
The order row is locked (FOR UPDATE) for every change. Repeating a finished action returns the
order unchanged (safe retries).
"""

from datetime import timedelta
from typing import Annotated

from app.db.models._common import utc_now
from app.db.models.sales import Order, Payment, Shipment
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from blissbiz_common.paging import page_query
from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from sales_service.ops import OrderOps, log
from sales_service.shop import order_out

CANCELLABLE = ("PENDING_PAYMENT", "CONFIRMED", "PACKED")


class ShipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    carrier: str | None = Field(default=None, max_length=60)
    tracking_number: str | None = Field(default=None, max_length=80)


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=255)


def install_order_routes(app: FastAPI, *, Ctx, Db, remote, settings, ops: OrderOps) -> None:
    paging = Depends(page_query())
    business, load = ops.business, ops.load

    def page_of(db, base, page_args):
        page, size = page_args
        total = db.scalar(select(func.count()).select_from(base.subquery()))
        rows = db.scalars(
            base.order_by(Order.placed_at.desc(), Order.id).limit(size).offset((page - 1) * size)
        ).all()
        return {
            "items": [order_out(o) for o in rows],
            "page": page,
            "page_size": size,
            "total": total,
        }

    # ---- owner: list and detail ----------------------------------------------------------
    @app.get("/internal/v1/orders")
    def list_orders(
        ctx: Ctx,
        db: Db,
        page_args: Annotated[tuple[int, int], paging],
        status: Annotated[str | None, Query(max_length=20)] = None,
        q: Annotated[str | None, Query(max_length=40)] = None,
    ):
        ctx.require_permission("sales.read")
        base = select(Order).where(Order.business_id == business(ctx))
        if status:
            base = base.where(Order.order_status == status.upper())
        if q:
            base = base.where(Order.order_number.startswith(q.strip().upper(), autoescape=True))
        return page_of(db, base, page_args)

    @app.get("/internal/v1/orders/{order_id}")
    def get_order(order_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("sales.read")
        return ops.detail(db, load(db, business(ctx), order_id), owner=True, ctx=ctx)

    @app.get("/internal/v1/orders/{order_id}/invoice")
    def order_invoice(order_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("invoices.read")
        return ops.invoice_for(db, ctx, load(db, business(ctx), order_id))

    # ---- owner: lifecycle ----------------------------------------------------------------
    @app.post("/internal/v1/orders/{order_id}/pack")
    def pack(order_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("sales.fulfil")
        o = load(db, business(ctx), order_id, lock=True)
        if o.order_status == "PACKED":
            return ops.detail(db, o, owner=True)
        if o.order_status != "CONFIRMED":
            ops.refuse(o, "packed")
        ops.supply(ctx, o.id, "extend", {"ttl_seconds": settings.packed_hold_seconds})
        o = load(db, business(ctx), order_id, lock=True)
        if o.order_status != "CONFIRMED":  # changed while we were away (e.g. cancelled)
            ops.refuse(o, "packed")
        now = utc_now()
        ops.history(db, o, ctx, "ORDER", "CONFIRMED", "PACKED", now=now)
        ops.history(db, o, ctx, "FULFILMENT", o.fulfilment_status, "PACKED", now=now)
        o.order_status, o.fulfilment_status, o.updated_at = "PACKED", "PACKED", now
        db.commit()
        return ops.detail(db, o, owner=True)

    @app.post("/internal/v1/orders/{order_id}/ship")
    def ship(order_id: str, body: ShipIn, ctx: Ctx, db: Db):
        ctx.require_permission("sales.fulfil")
        o = load(db, business(ctx), order_id, lock=True)
        if o.order_status in ("SHIPPED", "DELIVERED"):
            return ops.detail(db, o, owner=True, ctx=ctx)
        if o.order_status not in ("CONFIRMED", "PACKED"):
            ops.refuse(o, "shipped")
        if o.payment_method == "ONLINE" and o.payment_status != "PAID":
            raise ApiError(409, "not_paid", "This online order is not paid yet.")
        result = ops.supply(
            ctx, o.id, "commit", {"actor_type": ctx.actor_type, "actor_id": ctx.actor_id}
        )
        if result["committed_total"] == 0:
            raise ApiError(
                409, "stock_not_reserved", "This order holds no stock, so it cannot be shipped."
            )
        o = load(db, business(ctx), order_id, lock=True)
        now = utc_now()
        ops.history(db, o, ctx, "ORDER", o.order_status, "SHIPPED", now=now)
        ops.history(db, o, ctx, "FULFILMENT", o.fulfilment_status, "SHIPPED", now=now)
        db.add(
            Shipment(
                business_id=o.business_id,
                order_id=o.id,
                carrier=body.carrier,
                tracking_number=body.tracking_number,
                status="IN_TRANSIT",
                shipped_at=now,
            )
        )
        o.order_status, o.fulfilment_status, o.updated_at = "SHIPPED", "SHIPPED", now
        db.commit()
        try:
            ops.issue_invoice(db, ctx, o)
        except ApiError as exc:  # shipped is shipped; the invoice is retried on demand
            log.warning("invoice for order %s not issued yet: %s", order_id, exc.code)
        return ops.detail(db, load(db, business(ctx), order_id), owner=True, ctx=ctx)

    @app.post("/internal/v1/orders/{order_id}/deliver")
    def deliver(order_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("sales.fulfil")
        o = load(db, business(ctx), order_id, lock=True)
        if o.order_status == "DELIVERED":
            return ops.detail(db, o, owner=True, ctx=ctx)
        if o.order_status != "SHIPPED":
            ops.refuse(o, "delivered")
        now = utc_now()
        ops.history(db, o, ctx, "ORDER", "SHIPPED", "DELIVERED", now=now)
        ops.history(db, o, ctx, "FULFILMENT", o.fulfilment_status, "DELIVERED", now=now)
        for s in db.scalars(
            select(Shipment).where(Shipment.order_id == o.id, Shipment.delivered_at.is_(None))
        ):
            s.status, s.delivered_at = "DELIVERED", now
        if o.payment_method == "COD" and o.payment_status == "UNPAID":  # cash collected
            db.add(
                Payment(
                    business_id=o.business_id,
                    order_id=o.id,
                    provider="cod",
                    provider_order_id=f"cod_{o.id}",
                    provider_payment_id=f"cod_pay_{o.id}",
                    method="cash",
                    status="CAPTURED",
                    amount=o.grand_total,
                    currency=o.currency,
                    created_at=now,
                    updated_at=now,
                )
            )
            ops.history(db, o, ctx, "PAYMENT", "UNPAID", "PAID", "Cash collected on delivery", now)
            o.payment_status = "PAID"
        o.order_status, o.fulfilment_status, o.updated_at = "DELIVERED", "DELIVERED", now
        db.commit()
        return ops.detail(db, o, owner=True, ctx=ctx)

    @app.post("/internal/v1/orders/{order_id}/cancel")
    def cancel(order_id: str, body: CancelIn, ctx: Ctx, db: Db):
        ctx.require_permission("sales.fulfil")
        o = load(db, business(ctx), order_id, lock=True)
        if o.order_status == "CANCELLED":
            return ops.detail(db, o, owner=True)
        if o.order_status not in CANCELLABLE:
            ops.refuse(o, "cancelled")
        if o.payment_status in ("PAID", "PARTIALLY_REFUNDED"):
            ctx.require_permission("sales.refund")  # cancelling a paid order refunds it
        ops.cancel_order(db, ctx, o, body.reason or "Cancelled by the shop")
        return ops.detail(db, load(db, business(ctx), order_id), owner=True)

    # ---- customer: own orders only ------------------------------------------------------
    def customer(ctx: InternalContext) -> tuple[str, str]:
        b = business(ctx)
        if ctx.actor_type != "CUSTOMER" or not ctx.actor_id:
            raise ApiError(401, "authentication_required", "Please sign in to continue.")
        return b, ctx.actor_id

    @app.get("/internal/v1/my/orders")
    def my_orders(ctx: Ctx, db: Db, page_args: Annotated[tuple[int, int], paging]):
        b, me = customer(ctx)
        return page_of(
            db, select(Order).where(Order.business_id == b, Order.customer_id == me), page_args
        )

    @app.get("/internal/v1/my/orders/{order_id}")
    def my_order(order_id: str, ctx: Ctx, db: Db):
        b, me = customer(ctx)
        return ops.detail(db, load(db, b, order_id, customer=me), owner=False, ctx=ctx)

    @app.get("/internal/v1/my/orders/{order_id}/invoice")
    def my_invoice(order_id: str, ctx: Ctx, db: Db):
        b, me = customer(ctx)
        return ops.invoice_for(db, ctx, load(db, b, order_id, customer=me))

    @app.post("/internal/v1/my/orders/{order_id}/cancel")
    def my_cancel(order_id: str, body: CancelIn, ctx: Ctx, db: Db):
        """Shoppers may cancel before the order is packed, within the shop's cancel window."""
        b, me = customer(ctx)
        o = load(db, b, order_id, lock=True, customer=me)
        if o.order_status == "CANCELLED":
            return ops.detail(db, o, owner=False)
        if o.order_status not in ("CONFIRMED", "PENDING_PAYMENT"):
            raise ApiError(
                409,
                "invalid_transition",
                "This order is already being prepared; contact the shop to cancel.",
            )
        window = remote.call("store", "GET", "/internal/v1/policies", remote.service_ctx(ctx)).get(
            "cancel_window_hours"
        )
        if window is not None and utc_now() - o.placed_at > timedelta(hours=window):
            raise ApiError(
                409,
                "cancel_window_passed",
                f"Orders can be cancelled within {window} hours of placing them.",
            )
        ops.cancel_order(db, ctx, o, body.reason or "Cancelled by the customer")
        return ops.detail(db, load(db, b, order_id, customer=me), owner=False)
