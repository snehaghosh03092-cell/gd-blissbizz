"""Store service: which business a hostname belongs to, the published storefront, and the typed
shop policies (shipping fee, returns, cancellation window). The only reader/writer of the store
tables.

Hostname resolution is how a shopper's request finds its shop: the gateway asks
``/internal/v1/hosts/resolve`` and only ACTIVE domains match, so a removed or unverified domain
shows nothing.
"""

from typing import Annotated

from app.db.models.store import Domain, PolicyDocument, Storefront, StorePolicy
from app.db.session import get_db
from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError, register_exception_handlers
from blissbiz_common.fastapi import make_internal_dependency
from blissbiz_common.logging import configure_logging, install_request_id
from fastapi import Depends, FastAPI, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from store_service.setup import install_setup_routes

POLICY_FIELDS = (
    "timezone",
    "support_hours",
    "support_phone",
    "support_email",
    "refund_window_days",
    "refund_method",
    "return_window_days",
    "return_conditions",
    "shipping_days_min",
    "shipping_days_max",
    "free_shipping_over",
    "flat_shipping_fee",
    "warranty_days",
    "cancel_window_hours",
    "show_exact_stock",
)


class StoreSettings(ServiceSettings):
    service_name: str = "store"
    base_domain: str = "localhost"  # shops live at <slug>.<base_domain>


def normalise_host(host: str) -> str:
    """'Threads-Co.localhost:8000' -> 'threads-co.localhost' (ports and a trailing dot ignored)."""
    host = host.strip().lower()
    if host.startswith("["):  # IPv6 literal: never a storefront
        return ""
    return host.split(":", 1)[0].rstrip(".")


def _business(ctx: InternalContext) -> str:
    if ctx.business_id is None:
        raise ApiError(404, "not_found", "Not found.")
    return ctx.business_id


def policies_out(policy: StorePolicy | None) -> dict:
    if policy is None:
        return {name: None for name in POLICY_FIELDS} | {"show_exact_stock": False}
    out = {name: getattr(policy, name) for name in POLICY_FIELDS}
    for money in ("free_shipping_over", "flat_shipping_fee"):
        if out[money] is not None:
            out[money] = f"{out[money]:.2f}"
    return out


def create_app(settings: StoreSettings | None = None, configure_logs: bool = False) -> FastAPI:
    settings = settings or StoreSettings()
    if configure_logs:
        configure_logging(settings.service_name, settings.log_level)
    internal = make_internal_dependency(settings)
    app = FastAPI(
        title="BlissBiz store", docs_url=None, redoc_url=None, openapi_url="/openapi.json"
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

    @app.get("/internal/v1/hosts/resolve")
    def resolve_host(ctx: Ctx, db: Db, host: Annotated[str, Query(min_length=1, max_length=260)]):
        if ctx.actor_type != "SERVICE":
            raise ApiError(403, "forbidden", "You do not have permission to do that.")
        name = normalise_host(host)
        row = db.execute(
            select(Domain, Storefront.status)
            .outerjoin(Storefront, Storefront.business_id == Domain.business_id)
            .where(Domain.hostname == name, Domain.status == "ACTIVE")
        ).first()
        if row is None:
            raise ApiError(404, "store_not_found", "There is no shop at this address.")
        domain, storefront_status = row
        return {
            "business_id": domain.business_id,
            "hostname": domain.hostname,
            "storefront_status": storefront_status or "DRAFT",
        }

    @app.get("/internal/v1/policies")
    def policies(ctx: Ctx, db: Db):
        return policies_out(db.get(StorePolicy, _business(ctx)))

    @app.get("/internal/v1/storefront")
    def storefront(ctx: Ctx, db: Db):
        """What a shopper sees: only the *published* configuration and PUBLIC documents."""
        b = _business(ctx)
        sf = db.get(Storefront, b)
        if sf is None or sf.status != "PUBLISHED" or sf.published_config is None:
            raise ApiError(404, "store_not_found", "There is no shop at this address.")
        docs = db.scalars(
            select(PolicyDocument)
            .where(PolicyDocument.business_id == b, PolicyDocument.audience == "PUBLIC")
            .order_by(PolicyDocument.slug)
        ).all()
        return {
            "template_key": sf.published_config.get("template_key", sf.template_key),
            "theme": sf.published_config.get("theme", sf.theme),
            "config": {k: v for k, v in sf.published_config.items() if k not in {"template_key", "theme"}},
            "config_version": sf.config_version,
            "policies": policies_out(db.get(StorePolicy, b)),
            "documents": [{"slug": d.slug, "title": d.title, "body": d.body} for d in docs],
        }

    install_setup_routes(
        app, Ctx=Ctx, Db=Db, base_domain=settings.base_domain, policies_out=policies_out
    )
    return app


app = create_app(configure_logs=True)
