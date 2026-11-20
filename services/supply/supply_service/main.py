"""Supply service: warehouses, stock balances and the append-only ledger (read side in this step).

It is the only reader/writer of stock tables. ``variant_id`` is a plain reference to the catalog
service: asking for another tenant's variant simply finds nothing, because every query is
filtered by the business in the signed context.
"""

from collections import defaultdict
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import httpx
from app.db.models.supply import InventoryBalance, InventoryTransaction, Warehouse
from app.db.session import get_db
from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError, register_exception_handlers
from blissbiz_common.fastapi import make_internal_dependency
from blissbiz_common.logging import configure_logging, install_request_id
from blissbiz_common.paging import page_query
from blissbiz_common.remote import SyncRemote
from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from supply_service.purchasing import install_purchasing_routes
from supply_service.reservations import install_reservation_routes
from supply_service.stock_ops import install_stock_routes

REPO = Path(__file__).resolve().parents[3]


class SupplySettings(ServiceSettings):
    service_name: str = "supply"
    catalog_url: str = "http://127.0.0.1:8103"
    upload_dir: Path = REPO / "var" / "uploads"  # original supplier documents (outside the code)


class StockQuery(BaseModel):
    variant_ids: list[str] = Field(min_length=1, max_length=200)


def _business(ctx: InternalContext) -> str:
    if ctx.business_id is None:
        raise ApiError(404, "not_found", "Not found.")
    return ctx.business_id


def _qty(value: Decimal) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def create_app(
    settings: SupplySettings | None = None,
    transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    configure_logs: bool = False,
) -> FastAPI:
    settings = settings or SupplySettings()
    if configure_logs:
        configure_logging(settings.service_name, settings.log_level)
    internal = make_internal_dependency(settings)
    remote = SyncRemote(
        "supply",
        settings.internal_signing_secret.get_secret_value(),
        {"catalog": settings.catalog_url},
        transports,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await remote.aclose()

    app = FastAPI(
        title="BlissBiz supply",
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    def catalog_facts(
        ctx: InternalContext, variant_ids: list[str], allow_archived: bool = False
    ) -> dict:
        """Check with catalog that every variant belongs to this business (and is not archived)."""
        found: dict = {}
        ids = list(dict.fromkeys(variant_ids))
        for start in range(0, len(ids), 100):
            found.update(
                remote.call(
                    "catalog",
                    "POST",
                    "/internal/v1/public/quote",
                    remote.service_ctx(ctx),
                    json_body={"variant_ids": ids[start : start + 100]},
                )["items"]
            )
        bad = {v: "Unknown item." for v in ids if v not in found}
        if not allow_archived:
            bad |= {v: "This item is archived." for v in ids if v in found and found[v]["archived"]}
        if bad:
            raise ApiError(422, "validation_error", "Some items are not valid.", bad)
        return found

    install_request_id(app)
    register_exception_handlers(app)
    Ctx = Annotated[InternalContext, Depends(internal)]
    Db = Annotated[Session, Depends(get_db)]
    paging = Depends(page_query())

    @app.get("/health")
    def health():
        return {"status": "ok", "service": settings.service_name}

    @app.get("/internal/v1/health")
    def ready(ctx: Ctx, db: Db):
        db.execute(text("SELECT 1"))
        return {"status": "ready", "service": settings.service_name}

    @app.get("/internal/v1/warehouses")
    def warehouses(ctx: Ctx, db: Db):
        ctx.require_permission("inventory.read")
        b = _business(ctx)
        rows = db.scalars(
            select(Warehouse)
            .where(Warehouse.business_id == b, Warehouse.archived_at.is_(None))
            .order_by(Warehouse.code)
        ).all()
        return {
            "items": [
                {
                    "id": w.id,
                    "code": w.code,
                    "name": w.name,
                    "is_default": w.is_default,
                    "state_code": w.state_code,
                }
                for w in rows
            ]
        }

    @app.post("/internal/v1/stock/query")
    def stock(body: StockQuery, ctx: Ctx, db: Db):
        """Stock per variant across warehouses. Variants with no stock rows are reported as zero."""
        ctx.require_permission("inventory.read")
        b = _business(ctx)
        ids = list(dict.fromkeys(body.variant_ids))
        rows = db.execute(
            select(InventoryBalance, Warehouse)
            .join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id)
            .where(InventoryBalance.business_id == b, InventoryBalance.variant_id.in_(ids))
            .order_by(Warehouse.code)
        ).all()
        per: dict[str, list] = defaultdict(list)
        for balance, warehouse in rows:
            per[balance.variant_id].append((balance, warehouse))
        see_cost = ctx.has("inventory.manage")
        items = {}
        for vid in ids:
            parts = per.get(vid, [])
            on_hand = sum((bal.on_hand for bal, _ in parts), Decimal(0))
            reserved = sum((bal.reserved for bal, _ in parts), Decimal(0))
            entry = {
                "on_hand": _qty(on_hand),
                "reserved": _qty(reserved),
                "available": _qty(on_hand - reserved),
                "warehouses": [
                    {
                        "code": w.code,
                        "on_hand": _qty(bal.on_hand),
                        "reserved": _qty(bal.reserved),
                        "available": _qty(bal.on_hand - bal.reserved),
                    }
                    for bal, w in parts
                ],
            }
            if see_cost:
                entry["stock_value"] = (
                    f"{sum((bal.on_hand * bal.avg_cost for bal, _ in parts), Decimal(0)):.2f}"
                )
            items[vid] = entry
        return {"items": items}

    @app.get("/internal/v1/stock/ledger")
    def ledger(
        ctx: Ctx,
        db: Db,
        page_args: Annotated[tuple[int, int], paging],
        variant_id: Annotated[str, Query(min_length=1, max_length=36)],
    ):
        """Newest-first history of one variant. Read-only: the ledger is append-only by design."""
        ctx.require_permission("inventory.read")
        b = _business(ctx)
        page, size = page_args
        where = (
            InventoryTransaction.business_id == b,
            InventoryTransaction.variant_id == variant_id,
        )
        total = db.scalar(select(func.count()).select_from(InventoryTransaction).where(*where))
        rows = db.execute(
            select(InventoryTransaction, Warehouse.code)
            .join(Warehouse, Warehouse.id == InventoryTransaction.warehouse_id)
            .where(*where)
            .order_by(InventoryTransaction.id.desc())
            .limit(size)
            .offset((page - 1) * size)
        ).all()
        see_cost = ctx.has("inventory.manage")
        return {
            "items": [
                {
                    "id": t.id,
                    "type": t.txn_type,
                    "quantity": _qty(t.quantity_delta),
                    "warehouse": code,
                    "reason": t.reason_code,
                    "source_type": t.source_type,
                    "source_id": t.source_id,
                    "created_at": t.created_at.isoformat(),
                    **(
                        {"unit_cost": f"{t.unit_cost:.4f}"}
                        if see_cost and t.unit_cost is not None
                        else {}
                    ),
                }
                for t, code in rows
            ],
            "page": page,
            "page_size": size,
            "total": total,
        }

    @app.post("/internal/v1/public/availability")
    def availability(body: StockQuery, ctx: Ctx, db: Db):
        """What a shopper may know: sellable quantity per variant (no costs, no warehouses).
        Stock in archived warehouses is not sellable, exactly as reservations treat it."""
        b = _business(ctx)
        ids = list(dict.fromkeys(body.variant_ids))
        rows = db.execute(
            select(InventoryBalance.variant_id, InventoryBalance.on_hand, InventoryBalance.reserved)
            .join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id)
            .where(
                InventoryBalance.business_id == b,
                InventoryBalance.variant_id.in_(ids),
                Warehouse.archived_at.is_(None),
            )
        ).all()
        free = dict.fromkeys(ids, Decimal(0))
        for vid, on_hand, reserved in rows:
            free[vid] += max(Decimal(0), on_hand - reserved)
        return {"items": {vid: _qty(q) for vid, q in free.items()}}

    install_reservation_routes(app, Ctx=Ctx, Db=Db)
    install_stock_routes(app, Ctx=Ctx, Db=Db, catalog_facts=catalog_facts)
    install_purchasing_routes(
        app, Ctx=Ctx, Db=Db, catalog_facts=catalog_facts, upload_dir=Path(settings.upload_dir)
    )
    return app


app = create_app(configure_logs=True)
