"""Sales service: carts, cash-on-delivery checkout and the order lifecycle. Owns the sales tables.

It never reads another service's tables. Prices and tax facts come from catalog, stock is held
and taken through supply, tax arithmetic from billing, shipping rules from store and the seller's
state/GST flags from identity, all over signed internal calls.

Rule for every write: **call other services first, write locally last**, and never keep local
writes pending while another service is called. If the local write fails after stock was
reserved, the reservation is released (a compensating action), so nothing leaks.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import anyio
import httpx
from app.db.session import SessionLocal, get_db
from blissbiz_common.cache import TTLCache
from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError, register_exception_handlers
from blissbiz_common.fastapi import make_internal_dependency
from blissbiz_common.logging import configure_logging, install_request_id
from blissbiz_common.remote import SyncRemote
from fastapi import Depends, FastAPI
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from sales_service.ops import OrderOps
from sales_service.orders import install_order_routes
from sales_service.returns import install_return_routes
from sales_service.shop import install_shop_routes
from sales_service.sweeper import sweep

log = logging.getLogger("sales")


class SalesSettings(ServiceSettings):
    service_name: str = "sales"
    identity_url: str = "http://127.0.0.1:8101"
    store_url: str = "http://127.0.0.1:8102"
    catalog_url: str = "http://127.0.0.1:8103"
    supply_url: str = "http://127.0.0.1:8104"
    billing_url: str = "http://127.0.0.1:8106"
    # cash on delivery holds stock for this long; unpacked orders are then cancelled (sweeper.py)
    cod_reservation_seconds: int = Field(default=7 * 24 * 3600, ge=60)
    packed_hold_seconds: int = Field(
        default=14 * 24 * 3600, ge=60
    )  # packed: extended, never cancelled
    sweep_interval_seconds: float = Field(default=300, ge=0)  # 0 = no background sweeping
    sweep_batch: int = Field(default=200, ge=1, le=500)
    profile_cache_seconds: float = Field(default=60, ge=0)


class Remote(SyncRemote):
    """Signed clients for the five services sales depends on, plus a short profile cache."""

    def __init__(self, settings: SalesSettings, transports: dict[str, httpx.AsyncBaseTransport]):
        urls = {
            name: getattr(settings, f"{name}_url")
            for name in ("identity", "store", "catalog", "supply", "billing")
        }
        super().__init__(
            "sales", settings.internal_signing_secret.get_secret_value(), urls, transports
        )
        self.profiles = TTLCache(settings.profile_cache_seconds)

    def profile(self, ctx: InternalContext) -> dict:
        cached = self.profiles.get(ctx.business_id)
        if cached is None:
            path = f"/internal/v1/businesses/{ctx.business_id}/profile"
            cached = self.call("identity", "GET", path, self.service_ctx(ctx))
            self.profiles.set(ctx.business_id, cached)
        return cached


def create_app(
    settings: SalesSettings | None = None,
    transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    configure_logs: bool = False,
    session_factory=SessionLocal,
) -> FastAPI:
    settings = settings or SalesSettings()
    if configure_logs:
        configure_logging(settings.service_name, settings.log_level)
    internal = make_internal_dependency(settings)
    remote = Remote(settings, transports or {})
    ops = OrderOps(remote, settings)

    async def sweeping() -> None:
        await asyncio.sleep(min(30.0, settings.sweep_interval_seconds))
        while True:
            try:
                await anyio.to_thread.run_sync(sweep, ops, session_factory, settings)
            except Exception:  # never let the loop die
                log.exception("reservation sweep failed")
            await asyncio.sleep(settings.sweep_interval_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(sweeping()) if settings.sweep_interval_seconds > 0 else None
        yield
        if task:
            task.cancel()
        await remote.aclose()

    app = FastAPI(
        title="BlissBiz sales",
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    install_request_id(app)
    register_exception_handlers(app)
    Ctx = Annotated[InternalContext, Depends(internal)]
    Db = Annotated[Session, Depends(get_db)]

    @app.get("/health")
    def health():
        return {"status": "ok", "service": settings.service_name}

    @app.get("/internal/v1/health")
    def ready(ctx: Ctx, db: Db):
        db.execute(text("SELECT 1"))
        return {"status": "ready", "service": settings.service_name}

    install_shop_routes(app, Ctx=Ctx, Db=Db, remote=remote, settings=settings)
    install_order_routes(app, Ctx=Ctx, Db=Db, remote=remote, settings=settings, ops=ops)
    install_return_routes(app, Ctx=Ctx, Db=Db, remote=remote, ops=ops)

    @app.post("/internal/v1/jobs/expire-reservations")
    def expire_reservations(ctx: Ctx):
        """Run the expiry sweep now (the background loop does the same every few minutes)."""
        if ctx.actor_type != "SERVICE":
            raise ApiError(403, "forbidden", "You do not have permission to do that.")
        return sweep(ops, session_factory, settings, ctx.request_id)

    return app


app = create_app(configure_logs=True)
