"""Automatic expiry of held stock (docs/06 section 5).

Supply lists holds whose ``expires_at`` has passed; sales decides per order, because only sales
knows whether the order still matters:

* order gone (checkout crashed): the hold is released (EXPIRED);
* PENDING_PAYMENT: hold released, order CANCELLED, payment FAILED ("not received in time");
* CONFIRMED (cash on delivery, not packed): hold released, order CANCELLED;
* PACKED: never cancelled (the goods are boxed); the hold is extended instead;
* CANCELLED: a leftover hold is released;
* SHIPPED or later: left alone (stock was committed at dispatch).

Runs every ``sweep_interval_seconds`` inside the sales process (0 = off) and on demand through
``POST /internal/v1/jobs/expire-reservations``. Each order is handled in its own transaction,
so one bad order never blocks the rest.
"""

import logging

from app.db.models.sales import Order
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from sqlalchemy import select
from sqlalchemy.orm import Session

from sales_service.ops import OrderOps

log = logging.getLogger("sales.sweeper")


def sweep(ops: OrderOps, session_factory, settings, request_id: str = "sweeper") -> dict:
    remote = ops.remote
    me = InternalContext(request_id=request_id, actor_type="SERVICE", actor_id="sales")
    expired = remote.call(
        "supply",
        "GET",
        "/internal/v1/reservations-expired",
        me,
        params={"limit": settings.sweep_batch},
    )["items"]
    counts = {
        "checked": len(expired),
        "cancelled": 0,
        "released": 0,
        "extended": 0,
        "skipped": 0,
        "failed": 0,
    }
    for item in expired:
        ctx = InternalContext(
            request_id=request_id,
            actor_type="SERVICE",
            actor_id="sales",
            business_id=item["business_id"],
        )
        try:
            with session_factory() as db:
                outcome = handle(ops, db, ctx, item["order_id"], settings)
            counts[outcome] += 1
        except Exception:  # keep going; the next run retries this order
            log.exception("expiry of order %s failed", item["order_id"])
            counts["failed"] += 1
    if expired:
        log.info("reservation expiry: %s", counts)
    return counts


def handle(ops: OrderOps, db: Session, ctx: InternalContext, order_id: str, settings) -> str:
    o = db.scalar(
        select(Order)
        .where(Order.business_id == ctx.business_id, Order.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if o is None or o.order_status == "CANCELLED":
        ops.supply(ctx, order_id, "release", {"reason": "EXPIRED"})
        return "released"
    if o.order_status == "PACKED":
        ops.supply(ctx, order_id, "extend", {"ttl_seconds": settings.packed_hold_seconds})
        return "extended"
    if o.order_status in ("PENDING_PAYMENT", "CONFIRMED"):
        if o.payment_status in ("PAID", "PARTIALLY_REFUNDED"):
            return "skipped"  # paid orders are never cancelled by a timer
        reason = (
            "Payment not received in time"
            if o.order_status == "PENDING_PAYMENT"
            else "Not dispatched before the stock hold expired"
        )
        try:
            ops.cancel_order(db, ctx, o, reason, expired=True)
        except ApiError as exc:
            log.warning("order %s not expired: %s", order_id, exc.code)
            return "skipped"
        return "cancelled"
    return "skipped"
