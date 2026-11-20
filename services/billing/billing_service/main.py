"""Billing service (this step: tax quotes only; invoices come in a later step).

It owns ``tax_rate_versions``: which GST rate applied on which date. Checkout asks it for a
quote; the arithmetic is the shared exact-Decimal engine (``blissbiz_common.tax``), the same one
that produced the seeded orders, so live orders and seeded orders add up the same way.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated

from app.db.models import utc_now
from app.db.models.billing import TaxRateVersion
from app.db.session import get_db
from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError, register_exception_handlers
from blissbiz_common.fastapi import make_internal_dependency
from blissbiz_common.logging import configure_logging, install_request_id
from blissbiz_common.tax import Rate, quote
from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from billing_service.documents import install_document_routes

IST_OFFSET = timedelta(hours=5, minutes=30)


class BillingSettings(ServiceSettings):
    service_name: str = "billing"


class QuoteLine(BaseModel):
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=3)
    unit_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    tax_class: str = Field(min_length=1, max_length=10)


class QuoteIn(BaseModel):
    lines: list[QuoteLine] = Field(min_length=1, max_length=100)
    seller_registered: bool
    inter_state: bool
    include_tax: bool
    discount_pct: Decimal = Field(default=Decimal(0), ge=0, le=100)
    shipping: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=2)
    on: date | None = None


def rate_lookup(db: Session):
    """Effective-dated rates: the version whose [from, to] range (both inclusive) holds the date."""
    cache: dict = {}

    def lookup(tax_class: str, on: date) -> Rate | None:
        key = (tax_class, on)
        if key not in cache:
            row = db.scalar(
                select(TaxRateVersion)
                .where(
                    TaxRateVersion.tax_class == tax_class,
                    TaxRateVersion.effective_from <= on,
                    (TaxRateVersion.effective_to.is_(None)) | (TaxRateVersion.effective_to >= on),
                )
                .order_by(TaxRateVersion.effective_from.desc())
                .limit(1)
            )
            cache[key] = (
                None
                if row is None
                else Rate(row.id, row.cgst_rate, row.sgst_rate, row.igst_rate, row.cess_rate)
            )
        return cache[key]

    return lookup


def create_app(settings: BillingSettings | None = None, configure_logs: bool = False) -> FastAPI:
    settings = settings or BillingSettings()
    if configure_logs:
        configure_logging(settings.service_name, settings.log_level)
    internal = make_internal_dependency(settings)
    app = FastAPI(
        title="BlissBiz billing", docs_url=None, redoc_url=None, openapi_url="/openapi.json"
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

    @app.post("/internal/v1/tax/quote")
    def tax_quote(body: QuoteIn, ctx: Ctx, db: Db):
        if ctx.actor_type != "SERVICE":
            raise ApiError(403, "forbidden", "You do not have permission to do that.")
        on = body.on or (utc_now() + IST_OFFSET).date()  # GST dates are Indian dates
        result = quote(
            [(line.quantity, line.unit_price, line.tax_class) for line in body.lines],
            rates=rate_lookup(db),
            seller_registered=body.seller_registered,
            inter_state=body.inter_state,
            include_tax=body.include_tax,
            discount_pct=body.discount_pct,
            shipping_gross=body.shipping,
            on=on,
        )
        return {**result.as_json(), "on": on.isoformat()}

    install_document_routes(app, Ctx=Ctx, Db=Db, rate_lookup=rate_lookup)
    return app


app = create_app(configure_logs=True)
