"""Stock reservations (docs/06): the only way checkout and fulfilment change stock.

* reserve: all-or-nothing. Balance rows are locked (``SELECT ... FOR UPDATE``) in a fixed
  (variant, warehouse) order, so two shoppers buying the last unit queue up instead of both
  winning, and two checkouts can never deadlock each other. ``reserved`` grows, ``on_hand``
  does not change yet.
* commit (at dispatch for cash on delivery): ``on_hand`` and ``reserved`` both shrink and a SALE
  row is appended to the ledger, keyed ``sale:<reservation id>`` so a retry cannot sell twice.
* release (cancel/expiry): ``reserved`` shrinks; nothing is written to the ledger.

Only the sales service may call these endpoints. Each call is idempotent.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from app.db.models import utc_now
from app.db.models.supply import InventoryBalance, InventoryTransaction, StockReservation, Warehouse
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import FastAPI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ZERO = Decimal(0)


class Line(BaseModel):
    variant_id: str = Field(min_length=1, max_length=36)
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=3)


class ReserveIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=36)
    idempotency_key: str = Field(min_length=1, max_length=60)
    ttl_seconds: int = Field(ge=60, le=30 * 24 * 3600)
    lines: list[Line] = Field(min_length=1, max_length=100)


class CommitIn(BaseModel):
    actor_type: str = Field(default="SYSTEM", max_length=20)
    actor_id: str | None = Field(default=None, max_length=36)


class ExtendIn(BaseModel):
    ttl_seconds: int = Field(ge=60, le=90 * 24 * 3600)


class ReleaseIn(BaseModel):
    reason: Literal["CANCELLED", "EXPIRED"] = "CANCELLED"


def _qty(value: Decimal) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def reservation_out(r: StockReservation) -> dict:
    return {
        "id": r.id,
        "variant_id": r.variant_id,
        "warehouse_id": r.warehouse_id,
        "quantity": _qty(r.quantity),
        "status": r.status,
        "expires_at": r.expires_at.isoformat(),
    }


def lock_balances(db: Session, business_id: str, pairs_or_variants) -> list[InventoryBalance]:
    """Lock balance rows in one deterministic order (variant, warehouse) to avoid deadlocks."""
    query = select(InventoryBalance).where(InventoryBalance.business_id == business_id)
    query = query.where(InventoryBalance.variant_id.in_(sorted(pairs_or_variants)))
    query = query.order_by(InventoryBalance.variant_id, InventoryBalance.warehouse_id)
    return list(db.scalars(query.with_for_update().execution_options(populate_existing=True)))


def install_reservation_routes(app: FastAPI, *, Ctx, Db) -> None:
    def sales_only(ctx: InternalContext) -> str:
        if ctx.actor_type != "SERVICE" or ctx.actor_id != "sales" or ctx.business_id is None:
            raise ApiError(403, "forbidden", "You do not have permission to do that.")
        return ctx.business_id

    def existing(db: Session, b: str, order_id: str, statuses=("ACTIVE", "COMMITTED")):
        return list(
            db.scalars(
                select(StockReservation)
                .where(
                    StockReservation.business_id == b,
                    StockReservation.order_id == order_id,
                    StockReservation.status.in_(statuses),
                )
                .order_by(StockReservation.variant_id, StockReservation.warehouse_id)
            )
        )

    @app.post("/internal/v1/reservations", status_code=201)
    def reserve(body: ReserveIn, ctx: Ctx, db: Db):
        b = sales_only(ctx)
        already = existing(db, b, body.order_id)
        if already:  # a retry of the same checkout: hand back what it already holds
            return {
                "order_id": body.order_id,
                "replayed": True,
                "reservations": [reservation_out(r) for r in already],
            }
        wanted: dict[str, Decimal] = defaultdict(Decimal)
        for line in body.lines:
            wanted[line.variant_id] += line.quantity
        usable = {
            w.id: w
            for w in db.scalars(
                select(Warehouse).where(Warehouse.business_id == b, Warehouse.archived_at.is_(None))
            )
        }
        balances = [x for x in lock_balances(db, b, wanted) if x.warehouse_id in usable]
        per_variant: dict[str, list[InventoryBalance]] = defaultdict(list)
        for bal in balances:
            per_variant[bal.variant_id].append(bal)
        plan, short = [], {}
        for vid in sorted(wanted):
            rows = sorted(
                per_variant.get(vid, []),
                key=lambda x: (
                    not usable[x.warehouse_id].is_default,
                    -(x.on_hand - x.reserved),
                    usable[x.warehouse_id].code,
                ),
            )
            available = sum((max(ZERO, x.on_hand - x.reserved) for x in rows), ZERO)
            if available < wanted[vid]:
                short[vid] = _qty(available)
                continue
            need = wanted[vid]
            for bal in rows:
                take = min(need, bal.on_hand - bal.reserved)
                if take > 0:
                    plan.append((vid, bal, take))
                    need -= take
                if need == 0:
                    break
        if short:
            db.rollback()  # all-or-nothing: release the row locks, change nothing
            raise ApiError(
                409,
                "out_of_stock",
                "Some items are not available in the quantity requested.",
                short,
            )
        expires = utc_now() + timedelta(seconds=body.ttl_seconds)
        created, portions = [], defaultdict(int)
        for vid, bal, take in plan:
            portions[vid] += 1
            bal.reserved += take
            bal.version += 1
            r = StockReservation(
                business_id=b,
                order_id=body.order_id,
                variant_id=vid,
                warehouse_id=bal.warehouse_id,
                quantity=take,
                status="ACTIVE",
                expires_at=expires,
                idempotency_key=f"{body.idempotency_key}:{vid}:{portions[vid]}",
            )
            db.add(r)
            created.append(r)
        try:
            db.commit()
        except IntegrityError:  # the same checkout raced itself: the other attempt won
            db.rollback()
            already = existing(db, b, body.order_id)
            if not already:
                raise ApiError(409, "conflict", "Please try again.") from None
            return {
                "order_id": body.order_id,
                "replayed": True,
                "reservations": [reservation_out(r) for r in already],
            }
        return {
            "order_id": body.order_id,
            "replayed": False,
            "reservations": [reservation_out(r) for r in created],
        }

    @app.get("/internal/v1/reservations/{order_id}")
    def show(order_id: str, ctx: Ctx, db: Db):
        b = sales_only(ctx)
        rows = existing(db, b, order_id, ("ACTIVE", "COMMITTED", "RELEASED", "EXPIRED"))
        return {"order_id": order_id, "reservations": [reservation_out(r) for r in rows]}

    @app.post("/internal/v1/reservations/{order_id}/commit")
    def commit(order_id: str, body: CommitIn, ctx: Ctx, db: Db):
        """Stock leaves the shelf: ACTIVE reservations become SALE ledger rows."""
        b = sales_only(ctx)
        active = list(
            db.scalars(
                select(StockReservation)
                .where(
                    StockReservation.business_id == b,
                    StockReservation.order_id == order_id,
                    StockReservation.status == "ACTIVE",
                )
                .order_by(StockReservation.variant_id, StockReservation.warehouse_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if active:
            balances = {
                (x.variant_id, x.warehouse_id): x
                for x in lock_balances(db, b, {r.variant_id for r in active})
            }
            now = utc_now()
            for r in active:
                bal = balances[(r.variant_id, r.warehouse_id)]
                bal.reserved -= r.quantity
                bal.on_hand -= r.quantity
                bal.version += 1
                r.status = "COMMITTED"
                db.add(
                    InventoryTransaction(
                        business_id=b,
                        variant_id=r.variant_id,
                        warehouse_id=r.warehouse_id,
                        txn_type="SALE",
                        quantity_delta=-r.quantity,
                        unit_cost=bal.avg_cost,
                        reason_code="ORDER_DISPATCHED",
                        source_type="ORDER",
                        source_id=order_id,
                        idempotency_key=f"sale:{r.id}",
                        actor_type=body.actor_type,
                        actor_id=body.actor_id,
                        created_at=now,
                    )
                )
            db.commit()
        committed = existing(db, b, order_id, ("COMMITTED",))
        return {
            "order_id": order_id,
            "committed_now": len(active),
            "committed_total": len(committed),
        }

    @app.post("/internal/v1/reservations/{order_id}/extend")
    def extend(order_id: str, body: ExtendIn, ctx: Ctx, db: Db):
        """Packed goods wait for the courier: push the hold's expiry out (never shortens it)."""
        b = sales_only(ctx)
        until = utc_now() + timedelta(seconds=body.ttl_seconds)
        rows = list(
            db.scalars(
                select(StockReservation)
                .where(
                    StockReservation.business_id == b,
                    StockReservation.order_id == order_id,
                    StockReservation.status == "ACTIVE",
                )
                .with_for_update()
            )
        )
        for r in rows:
            if r.expires_at < until:
                r.expires_at = until
        db.commit()
        return {"order_id": order_id, "extended": len(rows), "until": until.isoformat()}

    @app.post("/internal/v1/reservations/{order_id}/release")
    def release(order_id: str, body: ReleaseIn, ctx: Ctx, db: Db):
        """Give held stock back (order cancelled or reservation expired)."""
        b = sales_only(ctx)
        active = list(
            db.scalars(
                select(StockReservation)
                .where(
                    StockReservation.business_id == b,
                    StockReservation.order_id == order_id,
                    StockReservation.status == "ACTIVE",
                )
                .order_by(StockReservation.variant_id, StockReservation.warehouse_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if active:
            balances = {
                (x.variant_id, x.warehouse_id): x
                for x in lock_balances(db, b, {r.variant_id for r in active})
            }
            for r in active:
                bal = balances[(r.variant_id, r.warehouse_id)]
                bal.reserved -= r.quantity
                bal.version += 1
                r.status = "RELEASED" if body.reason == "CANCELLED" else "EXPIRED"
            db.commit()
        committed = existing(db, b, order_id, ("COMMITTED",))
        return {
            "order_id": order_id,
            "released_now": len(active),
            "committed_total": len(committed),
        }
