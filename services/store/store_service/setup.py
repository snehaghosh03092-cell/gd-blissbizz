"""Owner-side storefront setup (step 12): template, colours, home-page text, shop policies, and
publishing. Publishing freezes the draft into ``published_config`` (shoppers only ever see the
published copy) and makes sure the shop's address ``<slug>.<base domain>`` exists and is ACTIVE.
"""

import re
from decimal import Decimal
from typing import Annotated, Literal

from app.db.models import utc_now
from app.db.models.store import TEMPLATE_KEYS, Domain, PolicyDocument, Storefront, StorePolicy
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

COLOR = r"^#[0-9a-fA-F]{6}$"
Days = Annotated[int, Field(ge=0, le=365)]
Money = Annotated[Decimal, Field(ge=0, le=1_000_000, max_digits=12, decimal_places=2)]
DEFAULT_THEMES = {
    "fashion": {"primary": "#1f2937", "accent": "#f472b6"},
    "grocery": {"primary": "#15803d", "accent": "#f59e0b"},
    "electronics": {"primary": "#0f172a", "accent": "#06b6d4"},
    "gaming": {"primary": "#4c1d95", "accent": "#22d3ee"},
    "food": {"primary": "#9a3412", "accent": "#eab308"},
    "beauty": {"primary": "#65374e", "accent": "#eeb3c5"},
    "craft": {"primary": "#704736", "accent": "#e5ae85"},
    "home": {"primary": "#3b5055", "accent": "#a9d9d3"},
}


class Theme(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary: str = Field(pattern=COLOR)
    accent: str = Field(pattern=COLOR)
    logo_text: str | None = Field(default=None, max_length=40)


class SetupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_key: Literal[TEMPLATE_KEYS]  # type: ignore[valid-type]
    theme: Theme | None = None
    hero_title: str = Field(min_length=1, max_length=120)
    hero_subtitle: str | None = Field(default=None, max_length=200)


class PoliciesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shipping_days_min: Days | None = None
    shipping_days_max: Days | None = None
    free_shipping_over: Money | None = None
    flat_shipping_fee: Money | None = None
    return_window_days: Days | None = None
    return_conditions: str | None = Field(default=None, max_length=1000)
    refund_method: str | None = Field(default=None, max_length=190)
    cancel_window_hours: Annotated[int, Field(ge=0, le=720)] | None = None
    support_email: str | None = Field(default=None, max_length=254)
    support_phone: str | None = Field(default=None, max_length=40)
    show_exact_stock: bool = False

    @model_validator(mode="after")
    def _range(self):
        if (
            self.shipping_days_min is not None
            and self.shipping_days_max is not None
            and self.shipping_days_min > self.shipping_days_max
        ):
            raise ValueError("Fastest delivery cannot be slower than slowest delivery.")
        return self


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")


def install_setup_routes(app: FastAPI, *, Ctx, Db, base_domain: str, policies_out) -> None:
    def business(ctx: InternalContext) -> str:
        if not ctx.business_id:
            raise ApiError(404, "not_found", "Not found.")
        return ctx.business_id

    def settings_out(db, b: str) -> dict:
        sf = db.get(Storefront, b)
        domains = db.scalars(
            select(Domain).where(Domain.business_id == b).order_by(Domain.created_at)
        ).all()
        return {
            "storefront": None
            if sf is None
            else {
                "template_key": sf.template_key,
                "status": sf.status,
                "theme": sf.theme,
                "draft_config": sf.draft_config,
                "published_config": sf.published_config,
                "config_version": sf.config_version,
                "published_at": sf.published_at.isoformat() if sf.published_at else None,
                "unpublished_changes": sf.published_config is not None
                and sf.published_config
                != {**sf.draft_config, "template_key": sf.template_key, "theme": sf.theme}
                and not ("template_key" not in sf.published_config and sf.published_config == sf.draft_config),
            },
            "policies": policies_out(db.get(StorePolicy, b)),
            "domains": [
                {"hostname": d.hostname, "status": d.status, "kind": d.kind} for d in domains
            ],
            "templates": list(TEMPLATE_KEYS),
        }

    @app.get("/internal/v1/storefront/settings")
    def get_settings(ctx: Ctx, db: Db):
        ctx.require_permission("storefront.read")
        return settings_out(db, business(ctx))

    @app.put("/internal/v1/storefront/setup")
    def setup(body: SetupIn, ctx: Ctx, db: Db):
        ctx.require_permission("storefront.write")
        b = business(ctx)
        theme = (body.theme.model_dump() if body.theme else None) or dict(
            DEFAULT_THEMES[body.template_key]
        )
        config = {
            "hero": {"title": body.hero_title, "subtitle": body.hero_subtitle or ""},
            "sections": ["hero", "featured", "categories", "about"],
            "show_stock_badges": True,
        }
        sf = db.get(Storefront, b)
        if sf is None:
            sf = Storefront(
                business_id=b,
                template_key=body.template_key,
                status="DRAFT",
                theme=theme,
                draft_config=config,
                config_version=1,
            )
            db.add(sf)
        else:
            if sf.published_config and "template_key" not in sf.published_config:
                sf.published_config = {
                    **sf.published_config,
                    "template_key": sf.template_key,
                    "theme": sf.theme,
                }
            sf.template_key, sf.theme, sf.draft_config = body.template_key, theme, config
        db.commit()
        return settings_out(db, b)

    @app.put("/internal/v1/policies")
    def put_policies(body: PoliciesIn, ctx: Ctx, db: Db):
        ctx.require_permission("storefront.write")
        b = business(ctx)
        p = db.get(StorePolicy, b)
        if p is None:
            p = StorePolicy(business_id=b, version=0)
            db.add(p)
        for key, value in body.model_dump().items():
            setattr(p, key, value)
        p.version = (p.version or 0) + 1
        docs = {
            "shipping": ("Shipping policy", _shipping_text(body)),
            "returns": ("Returns and refunds", _returns_text(body)),
        }
        for slug, (title, text) in docs.items():
            doc = db.scalar(
                select(PolicyDocument).where(
                    PolicyDocument.business_id == b, PolicyDocument.slug == slug
                )
            )
            if doc is None:
                db.add(
                    PolicyDocument(
                        business_id=b, slug=slug, title=title, audience="PUBLIC", body=text
                    )
                )
            else:
                doc.body, doc.version = text, doc.version + 1
        db.commit()
        return settings_out(db, b)

    @app.post("/internal/v1/storefront/publish")
    def publish(body: PublishIn, ctx: Ctx, db: Db):
        ctx.require_permission("storefront.publish")
        b = business(ctx)
        sf = db.get(Storefront, b)
        if sf is None or not sf.draft_config:
            raise ApiError(409, "store_not_set_up", "Choose a template and home-page text first.")
        if db.get(StorePolicy, b) is None:
            raise ApiError(409, "policies_missing", "Set your shipping and return policies first.")
        hostname = f"{body.slug}.{base_domain}"
        domain = db.scalar(select(Domain).where(Domain.hostname == hostname))
        if domain is not None and domain.business_id != b:
            raise ApiError(409, "hostname_taken", "This shop address belongs to another business.")
        now = utc_now()
        if domain is None:
            db.add(
                Domain(
                    business_id=b,
                    hostname=hostname,
                    kind="SUBDOMAIN",
                    status="ACTIVE",
                    provider="local",
                    last_checked_at=now,
                )
            )
        elif domain.status != "ACTIVE":
            domain.status, domain.last_checked_at = "ACTIVE", now
        config = {**sf.draft_config, "template_key": sf.template_key, "theme": sf.theme}
        if sf.status != "PUBLISHED" or sf.published_config != config:
            sf.published_config = config
            sf.status, sf.published_at, sf.unpublished_at = "PUBLISHED", now, None
            sf.config_version = (sf.config_version or 0) + 1
        db.commit()
        out = settings_out(db, b)
        out["url_host"] = hostname
        return out

    @app.post("/internal/v1/storefront/unpublish")
    def unpublish(ctx: Ctx, db: Db):
        ctx.require_permission("storefront.publish")
        b = business(ctx)
        sf = db.get(Storefront, b)
        if sf is not None and sf.status == "PUBLISHED":
            sf.status, sf.unpublished_at = "UNPUBLISHED", utc_now()
            db.commit()
        return settings_out(db, b)


def _shipping_text(p: PoliciesIn) -> str:
    parts = ["We ship across India."]
    if p.shipping_days_min is not None and p.shipping_days_max is not None:
        parts.append(f"Delivery takes {p.shipping_days_min} to {p.shipping_days_max} days.")
    if p.free_shipping_over is not None:
        parts.append(
            f"Orders of Rs {p.free_shipping_over:.0f} or more ship free; "
            "otherwise a flat fee of Rs "
            f"{(p.flat_shipping_fee or 0):.0f} applies."
        )
    return " ".join(parts)


def _returns_text(p: PoliciesIn) -> str:
    if not p.return_window_days:
        return "Returns are not accepted."
    text = f"Returns are accepted within {p.return_window_days} days of delivery."
    if p.return_conditions:
        text = f"{p.return_conditions.strip()} {text}"
    if p.refund_method:
        text += f" Refunds go to: {p.refund_method}."
    return re.sub(r"\s+", " ", text)
