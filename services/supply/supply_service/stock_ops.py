"""Owner stock operations (docs/06): warehouses, opening stock, adjustments, transfers,
reconciliation; plus the sales-only hooks for returns, cancellation restock and expiry.

Every change is a new row in the append-only ledger with a unique idempotency key, and the
balance row is locked (FOR UPDATE) while it changes. ``reserved`` stock (held for customers) is
never adjusted or transferred away: a change that would leave on_hand < reserved is refused.
"""

from collections import defaultdict
from decimal import Decimal
from typing import Annotated, Literal

from app.db.models import utc_now
from app.db.models.supply import (
    InventoryBalance,
    InventoryTransaction,
    ReconciliationDiscrepancy,
    ReconciliationRun,
    StockAdjustment,
    StockAdjustmentLine,
    StockReservation,
    StockTransfer,
    StockTransferLine,
    Warehouse,
)
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ZERO = Decimal(0)
LARGE_UNITS = Decimal(100)  # adjustments above this many units need inventory.adjust.large
Qty = Annotated[Decimal, Field(gt=0, le=1_000_000, max_digits=14, decimal_places=3)]
Cost = Annotated[Decimal, Field(ge=0, le=10_000_000, max_digits=14, decimal_places=4)]
Uuid = Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
REASONS = ("CYCLE_COUNT", "DAMAGED", "EXPIRED", "LOST", "FOUND", "CORRECTION", "OTHER")


def q3(value: Decimal) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def avg_after_inbound(bal: InventoryBalance, qty: Decimal, cost: Decimal) -> Decimal:
    """Weighted average cost after receiving ``qty`` at ``cost`` (4 decimal places)."""
    base = max(bal.on_hand, ZERO)
    total = base + qty
    if total <= 0:
        return cost
    return ((base * bal.avg_cost + qty * cost) / total).quantize(Decimal("0.0001"))


class WarehouseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{2,20}$")
    name: str = Field(min_length=1, max_length=100)
    state_code: str | None = Field(default=None, pattern=r"^[0-9]{2}$")
    address: str | None = Field(default=None, max_length=1000)
    is_default: bool = False


class OpeningLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant_id: str = Field(min_length=1, max_length=36)
    quantity: Qty
    unit_cost: Cost | None = None  # default: the variant's cost price from the catalog


class OpeningIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warehouse_id: str = Field(max_length=36)
    lines: list[OpeningLine] = Field(min_length=1, max_length=200)


class AdjustLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant_id: str = Field(min_length=1, max_length=36)
    warehouse_id: str = Field(max_length=36)
    counted_qty: Decimal | None = Field(
        default=None, ge=0, le=1_000_000, max_digits=14, decimal_places=3
    )
    delta: Decimal | None = Field(
        default=None, ge=-1_000_000, le=1_000_000, max_digits=14, decimal_places=3
    )

    @model_validator(mode="after")
    def _one(self):
        if (self.counted_qty is None) == (self.delta is None):
            raise ValueError("Give either counted_qty (what you counted) or delta (the change).")
        if self.delta is not None and self.delta == 0:
            raise ValueError("A change of 0 does nothing.")
        return self


class AdjustmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Uuid | None = (
        None  # client-chosen id: sending the same request twice creates one adjustment
    )
    reason_code: Literal[REASONS]  # type: ignore[valid-type]
    note: str | None = Field(default=None, max_length=500)
    post: bool = True  # False keeps it as a DRAFT count to review and post later
    lines: list[AdjustLine] = Field(min_length=1, max_length=200)


class TransferLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant_id: str = Field(min_length=1, max_length=36)
    quantity: Qty


class TransferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Uuid | None = None
    from_warehouse_id: str = Field(max_length=36)
    to_warehouse_id: str = Field(max_length=36)
    lines: list[TransferLine] = Field(min_length=1, max_length=200)


class ReturnLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant_id: str = Field(min_length=1, max_length=36)
    quantity: Qty
    condition: Literal["RESELLABLE", "DAMAGED"] = "RESELLABLE"
    key: str = Field(min_length=1, max_length=80)  # unique per returned line (idempotency)


class ReturnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    return_id: str = Field(max_length=36)
    lines: list[ReturnLine] = Field(min_length=1, max_length=100)


def install_stock_routes(app: FastAPI, *, Ctx, Db, catalog_facts) -> None:
    def business(ctx: InternalContext) -> str:
        if not ctx.business_id:
            raise ApiError(404, "not_found", "Not found.")
        return ctx.business_id

    def sales_only(ctx: InternalContext) -> None:
        if ctx.actor_type != "SERVICE" or ctx.actor_id != "sales":
            raise ApiError(403, "forbidden", "You do not have permission to do that.")

    def warehouse(db: Session, b: str, warehouse_id: str, field: str = "warehouse_id") -> Warehouse:
        w = db.scalar(
            select(Warehouse).where(
                Warehouse.business_id == b,
                Warehouse.id == warehouse_id,
                Warehouse.archived_at.is_(None),
            )
        )
        if w is None:
            raise ApiError(
                422, "validation_error", "Unknown warehouse.", {field: "Unknown warehouse."}
            )
        return w

    def balance(db: Session, b: str, variant_id: str, warehouse_id: str, create=True):
        """Locked balance row; created (zero) on first use."""
        query = (
            select(InventoryBalance)
            .where(
                InventoryBalance.business_id == b,
                InventoryBalance.variant_id == variant_id,
                InventoryBalance.warehouse_id == warehouse_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        bal = db.scalar(query)
        if bal is None and create:
            bal = InventoryBalance(
                business_id=b,
                variant_id=variant_id,
                warehouse_id=warehouse_id,
                on_hand=ZERO,
                reserved=ZERO,
                avg_cost=ZERO,
                version=0,
            )
            db.add(bal)
            db.flush()
        return bal

    def ledger(
        db,
        ctx,
        b,
        *,
        variant_id,
        warehouse_id,
        kind,
        delta,
        cost,
        reason,
        source,
        source_id,
        key,
        note=None,
    ):
        db.add(
            InventoryTransaction(
                business_id=b,
                variant_id=variant_id,
                warehouse_id=warehouse_id,
                txn_type=kind,
                quantity_delta=delta,
                unit_cost=cost,
                reason_code=reason,
                note=note,
                source_type=source,
                source_id=source_id,
                idempotency_key=key,
                actor_type=ctx.actor_type if ctx.actor_type != "SERVICE" else "SYSTEM",
                actor_id=ctx.actor_id if ctx.actor_type != "SERVICE" else None,
                created_at=utc_now(),
            )
        )

    def key_used(db: Session, b: str, key: str) -> bool:
        return (
            db.scalar(
                select(InventoryTransaction.id).where(
                    InventoryTransaction.business_id == b,
                    InventoryTransaction.idempotency_key == key,
                )
            )
            is not None
        )

    # ---- warehouses ---------------------------------------------------------------------------
    @app.post("/internal/v1/warehouses", status_code=201)
    def create_warehouse(body: WarehouseIn, ctx: Ctx, db: Db):
        ctx.require_permission("inventory.manage")
        b = business(ctx)
        code = body.code.upper()
        if db.scalar(
            select(Warehouse.id).where(Warehouse.business_id == b, Warehouse.code == code)
        ):
            raise ApiError(
                409, "code_taken", "A warehouse with this code exists.", {"code": "Already used."}
            )
        first = not db.scalar(
            select(func.count())
            .select_from(Warehouse)
            .where(Warehouse.business_id == b, Warehouse.archived_at.is_(None))
        )
        if body.is_default or first:
            db.execute(update(Warehouse).where(Warehouse.business_id == b).values(is_default=False))
        w = Warehouse(
            business_id=b,
            code=code,
            name=body.name,
            state_code=body.state_code,
            address=body.address,
            is_default=body.is_default or first,
        )
        db.add(w)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ApiError(409, "code_taken", "A warehouse with this code exists.") from None
        return {
            "id": w.id,
            "code": w.code,
            "name": w.name,
            "is_default": w.is_default,
            "state_code": w.state_code,
        }

    # ---- opening stock --------------------------------------------------------------------------
    @app.post("/internal/v1/stock/opening", status_code=201)
    def opening(body: OpeningIn, ctx: Ctx, db: Db):
        """First stock of a variant in a warehouse. Once a variant has history there, use a
        receipt (purchasing) or an adjustment instead."""
        ctx.require_permission("inventory.manage")
        b = business(ctx)
        ids = list(dict.fromkeys(line.variant_id for line in body.lines))
        if len(ids) != len(body.lines):
            raise ApiError(422, "validation_error", "Each variant may appear once.")
        facts = catalog_facts(ctx, ids)  # remote first: no local writes are pending
        w = warehouse(db, b, body.warehouse_id)
        done = []
        for line in sorted(body.lines, key=lambda x: x.variant_id):
            key = f"open:{line.variant_id}:{w.id}"
            history = db.scalar(
                select(func.count())
                .select_from(InventoryTransaction)
                .where(
                    InventoryTransaction.business_id == b,
                    InventoryTransaction.variant_id == line.variant_id,
                    InventoryTransaction.warehouse_id == w.id,
                )
            )
            if history:
                db.rollback()
                raise ApiError(
                    409,
                    "has_history",
                    "This item already has stock history here; use an adjustment or a receipt.",
                    {line.variant_id: "Has history."},
                )
            cost = (
                line.unit_cost
                if line.unit_cost is not None
                else Decimal(facts[line.variant_id]["cost_price"])
            )
            bal = balance(db, b, line.variant_id, w.id)
            bal.avg_cost = avg_after_inbound(bal, line.quantity, cost)
            bal.on_hand += line.quantity
            bal.version += 1
            ledger(
                db,
                ctx,
                b,
                variant_id=line.variant_id,
                warehouse_id=w.id,
                kind="OPENING",
                delta=line.quantity,
                cost=cost,
                reason="OPENING_STOCK",
                source="OPENING",
                source_id=None,
                key=key,
            )
            done.append({"variant_id": line.variant_id, "on_hand": q3(bal.on_hand)})
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ApiError(409, "has_history", "Opening stock was already recorded.") from None
        return {"warehouse_id": w.id, "items": done}

    # ---- adjustments ----------------------------------------------------------------------------
    def adjustment_out(db: Session, adj: StockAdjustment) -> dict:
        lines = db.scalars(
            select(StockAdjustmentLine)
            .where(StockAdjustmentLine.adjustment_id == adj.id)
            .order_by(StockAdjustmentLine.id)
        ).all()
        return {
            "id": adj.id,
            "reason_code": adj.reason_code,
            "note": adj.note,
            "status": adj.status,
            "created_at": adj.created_at.isoformat(),
            "posted_at": adj.posted_at.isoformat() if adj.posted_at else None,
            "lines": [
                {
                    "variant_id": ln.variant_id,
                    "warehouse_id": ln.warehouse_id,
                    "counted_qty": q3(ln.counted_qty) if ln.counted_qty is not None else None,
                    "delta": q3(ln.delta_qty),
                }
                for ln in lines
            ],
        }

    def post_adjustment(db: Session, ctx: InternalContext, adj: StockAdjustment) -> None:
        """Apply every line under row locks; refuse anything that breaks the stock rules."""
        lines = list(
            db.scalars(
                select(StockAdjustmentLine)
                .where(StockAdjustmentLine.adjustment_id == adj.id)
                .order_by(StockAdjustmentLine.variant_id, StockAdjustmentLine.warehouse_id)
            )
        )
        too_large, below, moved = {}, {}, 0
        for ln in lines:
            bal = balance(db, adj.business_id, ln.variant_id, ln.warehouse_id)
            delta = (ln.counted_qty - bal.on_hand) if ln.counted_qty is not None else ln.delta_qty
            ln.delta_qty = delta
            if delta == 0:
                continue
            if abs(delta) > LARGE_UNITS and not ctx.has("inventory.adjust.large"):
                too_large[ln.variant_id] = f"Changes over {LARGE_UNITS:g} units need an owner."
                continue
            if bal.on_hand + delta < bal.reserved:
                below[ln.variant_id] = (
                    f"Only {q3(max(bal.on_hand - bal.reserved, ZERO))} can be removed; "
                    "the rest is held for orders."
                )
                continue
            kind = (
                "DAMAGE"
                if adj.reason_code in ("DAMAGED", "EXPIRED") and delta < 0
                else "ADJUSTMENT"
            )
            bal.on_hand += delta
            bal.version += 1
            ledger(
                db,
                ctx,
                adj.business_id,
                variant_id=ln.variant_id,
                warehouse_id=ln.warehouse_id,
                kind=kind,
                delta=delta,
                cost=bal.avg_cost,
                reason=adj.reason_code,
                source="ADJUSTMENT",
                source_id=adj.id,
                key=f"adj:{adj.id}:{ln.id}",
                note=adj.note,
            )
            moved += 1
        if too_large:
            db.rollback()
            raise ApiError(
                403, "large_adjustment", "Large adjustments need the owner's approval.", too_large
            )
        if below:
            db.rollback()
            raise ApiError(409, "below_reserved", "That would remove stock held for orders.", below)
        adj.status, adj.posted_at, adj.approved_by = "POSTED", utc_now(), ctx.actor_id

    @app.get("/internal/v1/stock/adjustments")
    def list_adjustments(
        ctx: Ctx, db: Db, status: Annotated[str | None, Query(max_length=10)] = None
    ):
        ctx.require_permission("inventory.read")
        b = business(ctx)
        query = select(StockAdjustment).where(StockAdjustment.business_id == b)
        if status:
            query = query.where(StockAdjustment.status == status.upper())
        rows = db.scalars(query.order_by(StockAdjustment.created_at.desc()).limit(100)).all()
        return {"items": [adjustment_out(db, a) for a in rows]}

    @app.post("/internal/v1/stock/adjustments", status_code=201)
    def create_adjustment(body: AdjustmentIn, ctx: Ctx, db: Db):
        ctx.require_permission("inventory.adjust")
        b = business(ctx)
        if body.id:
            same = db.scalar(
                select(StockAdjustment).where(
                    StockAdjustment.business_id == b, StockAdjustment.id == body.id
                )
            )
            if same is not None:
                return {**adjustment_out(db, same), "replayed": True}
        pairs = [(ln.variant_id, ln.warehouse_id) for ln in body.lines]
        if len(set(pairs)) != len(pairs):
            raise ApiError(422, "validation_error", "Each item and warehouse may appear once.")
        catalog_facts(ctx, list({p[0] for p in pairs}), allow_archived=True)
        for ln in body.lines:
            warehouse(db, b, ln.warehouse_id)
        adj = StockAdjustment(
            business_id=b,
            reason_code=body.reason_code,
            note=body.note,
            status="DRAFT",
            created_by=ctx.actor_id,
            **({"id": body.id} if body.id else {}),
        )
        db.add(adj)
        db.flush()
        for ln in body.lines:
            db.add(
                StockAdjustmentLine(
                    adjustment_id=adj.id,
                    business_id=b,
                    variant_id=ln.variant_id,
                    warehouse_id=ln.warehouse_id,
                    counted_qty=ln.counted_qty,
                    delta_qty=ln.delta if ln.delta is not None else ZERO,
                )
            )
        db.flush()
        if body.post:
            post_adjustment(db, ctx, adj)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ApiError(409, "conflict", "Please try again.") from None
        return {**adjustment_out(db, adj), "replayed": False}

    @app.post("/internal/v1/stock/adjustments/{adjustment_id}/post")
    def post_draft(adjustment_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("inventory.adjust")
        b = business(ctx)
        adj = db.scalar(
            select(StockAdjustment)
            .where(StockAdjustment.business_id == b, StockAdjustment.id == adjustment_id)
            .with_for_update()
        )
        if adj is None:
            raise ApiError(404, "not_found", "Not found.")
        if adj.status == "POSTED":
            return adjustment_out(db, adj)
        post_adjustment(db, ctx, adj)
        db.commit()
        return adjustment_out(db, adj)

    # ---- transfers -----------------------------------------------------------------------------
    def transfer_out(db: Session, t: StockTransfer) -> dict:
        lines = db.scalars(
            select(StockTransferLine)
            .where(StockTransferLine.transfer_id == t.id)
            .order_by(StockTransferLine.variant_id)
        ).all()
        return {
            "id": t.id,
            "status": t.status,
            "from_warehouse_id": t.from_warehouse_id,
            "to_warehouse_id": t.to_warehouse_id,
            "created_at": t.created_at.isoformat(),
            "dispatched_at": t.dispatched_at.isoformat() if t.dispatched_at else None,
            "received_at": t.received_at.isoformat() if t.received_at else None,
            "lines": [{"variant_id": ln.variant_id, "quantity": q3(ln.quantity)} for ln in lines],
        }

    @app.get("/internal/v1/stock/transfers")
    def list_transfers(ctx: Ctx, db: Db):
        ctx.require_permission("inventory.read")
        rows = db.scalars(
            select(StockTransfer)
            .where(StockTransfer.business_id == business(ctx))
            .order_by(StockTransfer.created_at.desc())
            .limit(100)
        ).all()
        return {"items": [transfer_out(db, t) for t in rows]}

    @app.post("/internal/v1/stock/transfers", status_code=201)
    def create_transfer(body: TransferIn, ctx: Ctx, db: Db):
        """Dispatch now: stock leaves the source (TRANSFER_OUT) and is in transit until received."""
        ctx.require_permission("inventory.transfer")
        b = business(ctx)
        if body.id:
            same = db.scalar(
                select(StockTransfer).where(
                    StockTransfer.business_id == b, StockTransfer.id == body.id
                )
            )
            if same is not None:
                return {**transfer_out(db, same), "replayed": True}
        if body.from_warehouse_id == body.to_warehouse_id:
            raise ApiError(422, "validation_error", "Choose two different warehouses.")
        wanted: dict[str, Decimal] = defaultdict(Decimal)
        for ln in body.lines:
            wanted[ln.variant_id] += ln.quantity
        catalog_facts(ctx, list(wanted), allow_archived=True)
        src = warehouse(db, b, body.from_warehouse_id, "from_warehouse_id")
        dst = warehouse(db, b, body.to_warehouse_id, "to_warehouse_id")
        t = StockTransfer(
            business_id=b,
            from_warehouse_id=src.id,
            to_warehouse_id=dst.id,
            status="IN_TRANSIT",
            dispatched_at=utc_now(),
            **({"id": body.id} if body.id else {}),
        )
        db.add(t)
        db.flush()
        short = {}
        for vid in sorted(wanted):
            bal = balance(db, b, vid, src.id, create=False)
            free = (bal.on_hand - bal.reserved) if bal else ZERO
            if free < wanted[vid]:
                short[vid] = q3(max(free, ZERO))
                continue
            bal.on_hand -= wanted[vid]
            bal.version += 1
            db.add(
                StockTransferLine(
                    transfer_id=t.id, business_id=b, variant_id=vid, quantity=wanted[vid]
                )
            )
            ledger(
                db,
                ctx,
                b,
                variant_id=vid,
                warehouse_id=src.id,
                kind="TRANSFER_OUT",
                delta=-wanted[vid],
                cost=bal.avg_cost,
                reason="TRANSFER",
                source="TRANSFER",
                source_id=t.id,
                key=f"tro:{t.id}:{vid}",
            )
        if short:
            db.rollback()
            raise ApiError(
                409, "out_of_stock", "Not enough free stock in the source warehouse.", short
            )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ApiError(409, "conflict", "Please try again.") from None
        return {**transfer_out(db, t), "replayed": False}

    def finish_transfer(db: Session, ctx: InternalContext, transfer_id: str, cancel: bool) -> dict:
        b = business(ctx)
        t = db.scalar(
            select(StockTransfer)
            .where(StockTransfer.business_id == b, StockTransfer.id == transfer_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if t is None:
            raise ApiError(404, "not_found", "Not found.")
        target = "CANCELLED" if cancel else "RECEIVED"
        if t.status == target:
            return transfer_out(db, t)
        if t.status != "IN_TRANSIT":
            raise ApiError(
                409, "invalid_transition", f"This transfer is already {t.status.lower()}."
            )
        where = t.from_warehouse_id if cancel else t.to_warehouse_id
        prefix = "trc" if cancel else "tri"
        for ln in db.scalars(
            select(StockTransferLine)
            .where(StockTransferLine.transfer_id == t.id)
            .order_by(StockTransferLine.variant_id)
        ):
            out_row = db.scalar(
                select(InventoryTransaction).where(
                    InventoryTransaction.business_id == b,
                    InventoryTransaction.idempotency_key == f"tro:{t.id}:{ln.variant_id}",
                )
            )
            cost = out_row.unit_cost if out_row and out_row.unit_cost is not None else ZERO
            bal = balance(db, b, ln.variant_id, where)
            bal.avg_cost = avg_after_inbound(bal, ln.quantity, cost)
            bal.on_hand += ln.quantity
            bal.version += 1
            ledger(
                db,
                ctx,
                b,
                variant_id=ln.variant_id,
                warehouse_id=where,
                kind="TRANSFER_IN",
                delta=ln.quantity,
                cost=cost,
                reason="TRANSFER_CANCELLED" if cancel else "TRANSFER",
                source="TRANSFER",
                source_id=t.id,
                key=f"{prefix}:{t.id}:{ln.variant_id}",
            )
        t.status = target
        if not cancel:
            t.received_at = utc_now()
        db.commit()
        return transfer_out(db, t)

    @app.post("/internal/v1/stock/transfers/{transfer_id}/receive")
    def receive_transfer(transfer_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("inventory.transfer")
        return finish_transfer(db, ctx, transfer_id, cancel=False)

    @app.post("/internal/v1/stock/transfers/{transfer_id}/cancel")
    def cancel_transfer(transfer_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("inventory.transfer")
        return finish_transfer(db, ctx, transfer_id, cancel=True)

    # ---- reconciliation (report only; fixes are adjustments) ------------------------------------
    @app.post("/internal/v1/stock/reconcile", status_code=201)
    def reconcile(ctx: Ctx, db: Db):
        ctx.require_permission("inventory.reconcile")
        b = business(ctx)
        run = ReconciliationRun(business_id=b, status="RUNNING")
        db.add(run)
        db.flush()
        sums = dict(
            ((v, w), s)
            for v, w, s in db.execute(
                select(
                    InventoryTransaction.variant_id,
                    InventoryTransaction.warehouse_id,
                    func.sum(InventoryTransaction.quantity_delta),
                )
                .where(InventoryTransaction.business_id == b)
                .group_by(InventoryTransaction.variant_id, InventoryTransaction.warehouse_id)
            )
        )
        held = dict(
            ((v, w), s)
            for v, w, s in db.execute(
                select(
                    StockReservation.variant_id,
                    StockReservation.warehouse_id,
                    func.sum(StockReservation.quantity),
                )
                .where(StockReservation.business_id == b, StockReservation.status == "ACTIVE")
                .group_by(StockReservation.variant_id, StockReservation.warehouse_id)
            )
        )
        balances = db.scalars(
            select(InventoryBalance).where(InventoryBalance.business_id == b)
        ).all()
        found = []
        for bal in balances:
            key = (bal.variant_id, bal.warehouse_id)
            checks = (
                ("ON_HAND_MISMATCH", Decimal(sums.get(key) or 0), bal.on_hand),
                ("RESERVED_MISMATCH", Decimal(held.get(key) or 0), bal.reserved),
                ("NEGATIVE_AVAILABLE", bal.reserved, min(bal.on_hand, bal.reserved)),
            )
            for kind, expected, actual in checks:
                if expected != actual:
                    found.append(
                        ReconciliationDiscrepancy(
                            run_id=run.id,
                            business_id=b,
                            variant_id=bal.variant_id,
                            warehouse_id=bal.warehouse_id,
                            kind=kind,
                            expected=expected,
                            actual=actual,
                        )
                    )
        db.add_all(found)
        run.checked_count, run.discrepancy_count = len(balances), len(found)
        run.status, run.finished_at = "DONE", utc_now()
        db.commit()
        return run_out(db, run)

    def run_out(db: Session, run: ReconciliationRun) -> dict:
        rows = db.scalars(
            select(ReconciliationDiscrepancy).where(ReconciliationDiscrepancy.run_id == run.id)
        ).all()
        return {
            "id": run.id,
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "checked": run.checked_count,
            "discrepancies": [
                {
                    "variant_id": d.variant_id,
                    "warehouse_id": d.warehouse_id,
                    "kind": d.kind,
                    "expected": q3(d.expected),
                    "actual": q3(d.actual),
                }
                for d in rows
            ],
        }

    @app.get("/internal/v1/stock/reconcile")
    def reconcile_runs(ctx: Ctx, db: Db):
        ctx.require_permission("inventory.read")
        rows = db.scalars(
            select(ReconciliationRun)
            .where(ReconciliationRun.business_id == business(ctx))
            .order_by(ReconciliationRun.started_at.desc())
            .limit(10)
        ).all()
        return {"items": [run_out(db, r) for r in rows]}

    # ---- sales hooks: returns, cancellation restock, expiry -------------------------------------
    def committed(db: Session, b: str, order_id: str, variant_id: str | None = None):
        query = select(StockReservation).where(
            StockReservation.business_id == b,
            StockReservation.order_id == order_id,
            StockReservation.status == "COMMITTED",
        )
        if variant_id:
            query = query.where(StockReservation.variant_id == variant_id)
        return list(db.scalars(query.order_by(StockReservation.warehouse_id)))

    def sale_cost(db: Session, b: str, reservation_id: str, fallback: Decimal) -> Decimal:
        row = db.scalar(
            select(InventoryTransaction.unit_cost).where(
                InventoryTransaction.business_id == b,
                InventoryTransaction.idempotency_key == f"sale:{reservation_id}",
            )
        )
        return row if row is not None else fallback

    @app.post("/internal/v1/returns/{order_id}/receive")
    def receive_return(order_id: str, body: ReturnIn, ctx: Ctx, db: Db):
        """Returned goods come back into the warehouse they were shipped from (CUSTOMER_RETURN at
        their sale cost). Damaged goods are written off straight away (DAMAGE)."""
        sales_only(ctx)
        b = business(ctx)
        for line in sorted(body.lines, key=lambda x: x.key):
            if key_used(db, b, f"ret:{line.key}"):
                continue  # already received (retry)
            sources = committed(db, b, order_id, line.variant_id)
            if not sources:
                db.rollback()
                raise ApiError(
                    409,
                    "nothing_shipped",
                    "This item was not shipped from stock.",
                    {line.variant_id: "Not shipped."},
                )
            res = sources[0]
            bal = balance(db, b, line.variant_id, res.warehouse_id)
            cost = sale_cost(db, b, res.id, bal.avg_cost)
            bal.on_hand += line.quantity
            bal.version += 1
            ledger(
                db,
                ctx,
                b,
                variant_id=line.variant_id,
                warehouse_id=res.warehouse_id,
                kind="CUSTOMER_RETURN",
                delta=line.quantity,
                cost=cost,
                reason="CUSTOMER_RETURN",
                source="RETURN",
                source_id=body.return_id,
                key=f"ret:{line.key}",
            )
            if line.condition == "DAMAGED":
                bal.on_hand -= line.quantity
                ledger(
                    db,
                    ctx,
                    b,
                    variant_id=line.variant_id,
                    warehouse_id=res.warehouse_id,
                    kind="DAMAGE",
                    delta=-line.quantity,
                    cost=cost,
                    reason="RETURN_DAMAGED",
                    source="RETURN",
                    source_id=body.return_id,
                    key=f"retdmg:{line.key}",
                )
        db.commit()
        return {"order_id": order_id, "received": len(body.lines)}

    @app.post("/internal/v1/reservations/{order_id}/restock")
    def restock(order_id: str, ctx: Ctx, db: Db):
        """A paid order is cancelled after its stock was committed (online orders commit at
        payment): put it back (CUSTOMER_RETURN, reason ORDER_CANCELLED) and close the holds."""
        sales_only(ctx)
        b = business(ctx)
        done = 0
        for res in committed(db, b, order_id):
            bal = balance(db, b, res.variant_id, res.warehouse_id)
            if not key_used(db, b, f"cancel:{res.id}"):
                cost = sale_cost(db, b, res.id, bal.avg_cost)
                bal.on_hand += res.quantity
                bal.version += 1
                ledger(
                    db,
                    ctx,
                    b,
                    variant_id=res.variant_id,
                    warehouse_id=res.warehouse_id,
                    kind="CUSTOMER_RETURN",
                    delta=res.quantity,
                    cost=cost,
                    reason="ORDER_CANCELLED",
                    source="ORDER",
                    source_id=order_id,
                    key=f"cancel:{res.id}",
                )
                done += 1
            res.status = "RELEASED"
        db.commit()
        return {"order_id": order_id, "restocked": done}

    @app.get("/internal/v1/reservations-expired")
    def expired(ctx: Ctx, db: Db, limit: Annotated[int, Query(ge=1, le=500)] = 200):
        """(business, order) pairs holding ACTIVE reservations past their expiry, oldest first.
        Only sales decides what to do with them (it knows whether the order still matters)."""
        sales_only(ctx)
        rows = db.execute(
            select(
                StockReservation.business_id,
                StockReservation.order_id,
                func.min(StockReservation.expires_at),
            )
            .where(StockReservation.status == "ACTIVE", StockReservation.expires_at < utc_now())
            .group_by(StockReservation.business_id, StockReservation.order_id)
            .order_by(func.min(StockReservation.expires_at))
            .limit(limit)
        ).all()
        return {
            "items": [
                {"business_id": b, "order_id": o, "expired_at": e.isoformat()} for b, o, e in rows
            ]
        }
