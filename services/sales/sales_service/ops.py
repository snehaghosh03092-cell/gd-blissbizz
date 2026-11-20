"""Order operations shared by the owner routes, the customer routes, returns and the expiry
sweeper: loading, detail views, history, invoices, cancellation and refunds.

Rule kept everywhere: remote calls first, local writes last, never local writes pending while
another service is called. Every step is idempotent, so a retry after any failure is safe.
"""

import logging
from decimal import Decimal

from app.db.models._common import utc_now
from app.db.models.sales import (
    Order,
    OrderItem,
    OrderStatusHistory,
    Payment,
    Refund,
    ReturnRequest,
    ReturnRequestItem,
    Shipment,
)
from blissbiz_common.clients import raise_for
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from blissbiz_common.fy import india_date
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sales_service.shop import order_out, qty_str

log = logging.getLogger("sales")
ZERO = Decimal(0)
SHIPPED_OR_LATER = ("SHIPPED", "DELIVERED", "RETURN_REQUESTED", "RETURNED")
OPEN_RETURN = ("REQUESTED", "APPROVED", "RECEIVED")


def money(value) -> str:
    return f"{value:.2f}"


class OrderOps:
    def __init__(self, remote, settings):
        self.remote, self.settings = remote, settings

    # ---- reading ----------------------------------------------------------------------------
    @staticmethod
    def business(ctx: InternalContext) -> str:
        if ctx.business_id is None:
            raise ApiError(404, "not_found", "Not found.")
        return ctx.business_id

    @staticmethod
    def load(db: Session, b: str, order_id: str, *, lock=False, customer=None) -> Order:
        query = select(Order).where(Order.business_id == b, Order.id == order_id)
        if customer is not None:
            query = query.where(Order.customer_id == customer)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        order = db.scalar(query)
        if order is None:
            raise ApiError(404, "not_found", "Not found.")  # also another tenant's/customer's order
        return order

    def returns_of(self, db: Session, order_id: str) -> list[dict]:
        out = []
        for r in db.scalars(
            select(ReturnRequest)
            .where(ReturnRequest.order_id == order_id)
            .order_by(ReturnRequest.created_at)
        ):
            items = db.scalars(
                select(ReturnRequestItem).where(ReturnRequestItem.return_id == r.id)
            ).all()
            refund = db.scalar(
                select(Refund).where(
                    Refund.order_id == order_id, Refund.idempotency_key == f"return:{r.id}"
                )
            )
            out.append(
                {
                    "id": r.id,
                    "status": r.status,
                    "reason": r.reason,
                    "created_at": r.created_at.isoformat(),
                    "refund": money(refund.amount) if refund else None,
                    "items": [
                        {
                            "id": i.id,
                            "order_item_id": i.order_item_id,
                            "quantity": qty_str(i.quantity),
                            "condition": i.item_condition,
                        }
                        for i in items
                    ],
                }
            )
        return out

    def detail(
        self, db: Session, o: Order, *, owner: bool, ctx: InternalContext | None = None
    ) -> dict:
        items = list(
            db.scalars(select(OrderItem).where(OrderItem.order_id == o.id).order_by(OrderItem.sku))
        )
        history = db.scalars(
            select(OrderStatusHistory)
            .where(OrderStatusHistory.order_id == o.id)
            .order_by(OrderStatusHistory.id)
        ).all()
        shipments = db.scalars(select(Shipment).where(Shipment.order_id == o.id)).all()
        refunds = db.scalars(
            select(Refund).where(Refund.order_id == o.id).order_by(Refund.created_at)
        ).all()
        extra = {
            "history": [
                {
                    "axis": h.axis,
                    "from": h.from_status,
                    "to": h.to_status,
                    "actor": h.actor_type,
                    "note": h.note,
                    "at": h.created_at.isoformat(),
                }
                for h in history
            ],
            "shipments": [
                {
                    "carrier": s.carrier,
                    "tracking_number": s.tracking_number,
                    "status": s.status,
                    "shipped_at": s.shipped_at.isoformat() if s.shipped_at else None,
                    "delivered_at": s.delivered_at.isoformat() if s.delivered_at else None,
                }
                for s in shipments
            ],
            "returns": self.returns_of(db, o.id),
            "refunds": [
                {
                    "amount": money(r.amount),
                    "status": r.status,
                    "reason": r.reason,
                    "at": r.created_at.isoformat(),
                }
                for r in refunds
            ],
            "invoice": None,
        }
        if ctx is not None and o.order_status in SHIPPED_OR_LATER:
            extra["invoice"] = self.invoice_summary(ctx, o)
        if owner:
            payments = db.scalars(select(Payment).where(Payment.order_id == o.id)).all()
            extra["payments"] = [
                {
                    "provider": p.provider,
                    "method": p.method,
                    "status": p.status,
                    "amount": money(p.amount),
                }
                for p in payments
            ]
        return order_out(o, items, **extra)

    @staticmethod
    def history(db, o: Order, ctx: InternalContext, axis: str, frm, to, note=None, now=None):
        actor = ctx.actor_type if ctx.actor_type in ("OWNER", "CUSTOMER") else "SYSTEM"
        db.add(
            OrderStatusHistory(
                business_id=o.business_id,
                order_id=o.id,
                axis=axis,
                from_status=frm,
                to_status=to,
                actor_type=actor,
                actor_id=ctx.actor_id if actor != "SYSTEM" else None,
                note=note,
                created_at=now or utc_now(),
            )
        )

    @staticmethod
    def refuse(o: Order, action: str):
        raise ApiError(
            409,
            "invalid_transition",
            f"An order that is {o.order_status.lower().replace('_', ' ')} cannot be {action}.",
        )

    def supply(
        self, ctx: InternalContext, order_id: str, action: str, body: dict | None = None
    ) -> dict:
        return self.remote.call(
            "supply",
            "POST",
            f"/internal/v1/reservations/{order_id}/{action}",
            self.remote.service_ctx(ctx),
            json_body=body or {},
        )

    # ---- invoices ------------------------------------------------------------------------------
    def invoice_payload(self, db: Session, ctx: InternalContext, o: Order) -> dict:
        profile = self.remote.profile(ctx)
        items = list(
            db.scalars(select(OrderItem).where(OrderItem.order_id == o.id).order_by(OrderItem.sku))
        )
        ship = o.ship_to or {}
        seller_state = profile.get("state_code")
        address = ", ".join(
            x
            for x in (
                ship.get("line1"),
                ship.get("line2"),
                ship.get("city"),
                ship.get("postal_code"),
            )
            if x
        )
        return {
            "order_id": o.id,
            "customer_id": o.customer_id,
            "placed_on": india_date(o.placed_at).isoformat(),
            "seller": {
                "name": profile["name"],
                "legal_name": profile.get("legal_name"),
                "gstin": profile.get("gstin"),
                "address": profile.get("address"),
                "state_code": seller_state,
            },
            "buyer": {
                "name": ship.get("recipient") or "Customer",
                "address": address or None,
                "state_code": ship.get("state_code"),
                "gstin": ship.get("gstin"),
            },
            "gst_registered": profile["gst_registered"],
            "prices_include_tax": profile["prices_include_tax"],
            "invoice_prefix": profile["invoice_prefix"],
            "fy_start_month": profile.get("fy_start_month") or 4,
            "place_of_supply_state": o.place_of_supply_state,
            "inter_state": bool(
                o.place_of_supply_state and seller_state and o.place_of_supply_state != seller_state
            ),
            "shipping_total": money(o.shipping_total),
            "round_off": money(o.round_off),
            "grand_total": money(o.grand_total),
            "lines": [
                {
                    "description": (
                        i.product_name
                        + (
                            f" ({', '.join(str(v) for v in i.options.values())})"
                            if i.options
                            else ""
                        )
                    )[:300],
                    "hsn_sac": i.hsn_sac,
                    "quantity": str(i.quantity),
                    "unit": i.stock_unit,
                    "unit_price": money(i.unit_price),
                    "discount": money(i.discount),
                    "taxable": money(i.taxable_value),
                    "cgst": money(i.cgst),
                    "sgst": money(i.sgst),
                    "igst": money(i.igst),
                    "cess": money(i.cess),
                    "tax_rate_version_id": i.tax_rate_version_id,
                    "line_total": money(i.line_total),
                }
                for i in items
            ],
        }

    def issue_invoice(self, db: Session, ctx: InternalContext, o: Order) -> dict:
        payload = self.invoice_payload(db, ctx, o)
        db.rollback()  # end the read transaction: nothing local is pending while billing works
        return self.remote.call(
            "billing",
            "POST",
            "/internal/v1/invoices",
            self.remote.service_ctx(ctx),
            json_body=payload,
        )

    def invoice_for(self, db: Session, ctx: InternalContext, o: Order) -> dict:
        """The order's invoice; issued now if dispatch happened but issuing failed back then."""
        if o.order_status not in SHIPPED_OR_LATER:
            raise ApiError(404, "invoice_not_found", "Invoices are issued when the order ships.")
        response = self.remote.response(
            "billing", "GET", f"/internal/v1/invoices/by-order/{o.id}", self.remote.service_ctx(ctx)
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code != 404:
            raise_for(response)
        return self.issue_invoice(db, ctx, o)

    def invoice_summary(self, ctx: InternalContext, o: Order) -> dict | None:
        try:
            r = self.remote.response(
                "billing",
                "GET",
                f"/internal/v1/invoices/by-order/{o.id}",
                self.remote.service_ctx(ctx),
            )
        except ApiError:
            return {"pending": True, "unavailable": True}
        if r.status_code != 200:
            return {"pending": True}
        inv = r.json()
        return {
            "id": inv["id"],
            "number": inv["number"],
            "doc_type": inv["doc_type"],
            "grand_total": inv["grand_total"],
            "credit_notes": inv.get("credit_notes", []),
        }

    # ---- payments and refunds -----------------------------------------------------------------
    @staticmethod
    def captured(db: Session, o: Order) -> Payment | None:
        return db.scalar(
            select(Payment)
            .where(
                Payment.order_id == o.id,
                Payment.status.in_(("CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED")),
            )
            .order_by(Payment.created_at.desc())
            .limit(1)
        )

    @staticmethod
    def refunded(db: Session, payment: Payment) -> Decimal:
        return Decimal(
            db.scalar(
                select(func.coalesce(func.sum(Refund.amount), 0)).where(
                    Refund.payment_id == payment.id, Refund.status == "SUCCEEDED"
                )
            )
            or 0
        )

    def record_refund(
        self,
        db: Session,
        ctx: InternalContext,
        o: Order,
        amount: Decimal,
        *,
        key: str,
        reason: str,
        reference: str | None = None,
        now=None,
    ) -> Refund | None:
        """Refunds are recorded by hand (cash / bank / UPI: no payment gateway yet) and can
        never exceed what was collected."""
        payment = self.captured(db, o)
        if payment is None:
            raise ApiError(409, "nothing_to_refund", "No payment was collected for this order.")
        same = db.scalar(
            select(Refund).where(Refund.business_id == o.business_id, Refund.idempotency_key == key)
        )
        if same is not None:
            return same
        before = self.refunded(db, payment)
        amount = min(amount, payment.amount - before)
        if amount <= 0:
            return None
        now = now or utc_now()
        refund = Refund(
            business_id=o.business_id,
            payment_id=payment.id,
            order_id=o.id,
            amount=amount,
            reason=reason[:255],
            status="SUCCEEDED",
            idempotency_key=key,
            provider_refund_id=reference,
            created_by=ctx.actor_id,
            created_at=now,
        )
        db.add(refund)
        total = before + amount
        status = "REFUNDED" if total >= payment.amount else "PARTIALLY_REFUNDED"
        payment.status, payment.updated_at = status, now
        if o.payment_status != status:
            self.history(db, o, ctx, "PAYMENT", o.payment_status, status, reason[:255], now)
            o.payment_status = status
        return refund

    # ---- cancellation ---------------------------------------------------------------------------
    def cancel_order(
        self, db: Session, ctx: InternalContext, o: Order, reason: str, *, expired: bool = False
    ) -> None:
        """Give back held stock; a paid order also gets its stock restocked and its money
        refunded in full. The order row is locked by the caller."""
        paid = o.payment_status in ("PAID", "PARTIALLY_REFUNDED")
        if paid and self.captured(db, o) is None:  # check before touching stock
            raise ApiError(
                409, "nothing_to_refund", "The order is marked paid but no payment is recorded."
            )
        result = self.supply(
            ctx, o.id, "release", {"reason": "EXPIRED" if expired else "CANCELLED"}
        )
        if result["committed_total"] > 0:
            if not paid:  # COD stock only leaves at dispatch: committed + unpaid means shipped
                raise ApiError(
                    409,
                    "stock_already_committed",
                    "Stock for this order has already been dispatched.",
                )
            self.supply(ctx, o.id, "restock")  # online orders commit at payment: put it back
        o = self.load(db, o.business_id, o.id, lock=True)  # fresh lock after the remote calls
        now = utc_now()
        if paid:
            self.record_refund(
                db,
                ctx,
                o,
                o.grand_total,
                key=f"cancel:{o.id}",
                reason=f"Order cancelled: {reason}",
                now=now,
            )
        elif o.payment_status == "PENDING" and expired:
            self.history(
                db, o, ctx, "PAYMENT", "PENDING", "FAILED", "Payment not received in time", now
            )
            o.payment_status = "FAILED"
            for p in db.scalars(
                select(Payment).where(
                    Payment.order_id == o.id, Payment.status.in_(("CREATED", "PENDING"))
                )
            ):
                p.status, p.failure_code, p.updated_at = "FAILED", "RESERVATION_EXPIRED", now
        self.history(db, o, ctx, "ORDER", o.order_status, "CANCELLED", reason[:255], now)
        o.order_status, o.cancelled_at, o.updated_at = "CANCELLED", now, now
        o.cancel_reason = reason[:255]
        db.commit()
