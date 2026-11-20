"""Catalog editing (docs/05 section 3, needs ``catalog.write``): categories, products, variants.

Rules that keep the shop consistent:
* SKUs are unique per business; a product's variants must differ in their options.
* A product is published only with a description-free minimum: HSN/SAC code, a current tax
  class and at least one active variant with a price. Retired GST classes (12%/28%) cannot be
  chosen for new or edited products.
* The last active variant of a published product cannot be deactivated or archived (unpublish
  first), so the shop never shows a product nobody can buy.
* The stock unit is fixed once a product has been published (stock is counted in it).
* Changing a price never changes past orders (orders keep their own snapshot).
"""

import hashlib
import json
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from app.db.models import utc_now
from app.db.models.catalog import Category, Product, ProductVariant
from app.db.models.platform import IdempotencyKey
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import FastAPI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

CURRENT_TAX_CLASSES = ("GST_0", "GST_5", "GST_18", "GST_40", "EXEMPT", "NONE")
UNITS = ("PCS", "KG", "G", "L", "ML", "PACK", "BOX", "DOZEN", "PAIR")
HSN = re.compile(r"^[0-9]{4}([0-9]{2}){0,2}$")
SKU = r"^[A-Za-z0-9._-]{1,60}$"
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Price = Annotated[Decimal, Field(gt=0, le=10_000_000, max_digits=12, decimal_places=2)]
Cost = Annotated[Decimal, Field(ge=0, le=10_000_000, max_digits=14, decimal_places=4)]
Threshold = Annotated[Decimal, Field(ge=0, le=1_000_000, max_digits=14, decimal_places=3)]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:90] or "item"


def check_options(value: dict[str, str]) -> dict[str, str]:
    if len(value) > 5:
        raise ValueError("At most 5 options (for example size and colour).")
    clean = {}
    for key, val in value.items():
        k = key.strip().lower()
        v = str(val).strip()
        if not re.fullmatch(r"[a-z][a-z_]{0,19}", k):
            raise ValueError(f"Option name '{key}' should be a short word like 'size'.")
        if not v or len(v) > 40:
            raise ValueError(f"Option '{k}' needs a value of 1-40 characters.")
        clean[k] = v
    return clean


def option_hash(options: dict[str, str]) -> str:
    canonical = json.dumps(
        {k: v.lower() for k, v in sorted(options.items())}, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class CategoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=100)
    parent_id: str | None = Field(default=None, max_length=36)
    position: int = Field(default=0, ge=0, le=10_000)


class CategoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
        | None
    ) = None
    position: int | None = Field(default=None, ge=0, le=10_000)
    archived: bool | None = None


class ProductIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Text
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=200)
    category_id: str | None = Field(default=None, max_length=36)
    description: str | None = Field(default=None, max_length=5000)
    brand: str | None = Field(default=None, max_length=100)
    hsn_sac: str | None = None
    tax_class: Literal[CURRENT_TAX_CLASSES] = "NONE"  # type: ignore[valid-type]
    stock_unit: Literal[UNITS] = "PCS"  # type: ignore[valid-type]

    @field_validator("hsn_sac")
    @classmethod
    def _hsn(cls, value):
        if value in (None, ""):
            return None
        if not HSN.match(value.strip()):
            raise ValueError("HSN/SAC codes have 4, 6 or 8 digits.")
        return value.strip()


class ProductPatch(ProductIn):
    name: Text | None = None  # type: ignore[assignment]
    tax_class: Literal[CURRENT_TAX_CLASSES] | None = None  # type: ignore[valid-type,assignment]
    stock_unit: Literal[UNITS] | None = None  # type: ignore[valid-type,assignment]


class VariantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(pattern=SKU)
    options: dict[str, str] = Field(default_factory=dict)
    selling_price: Price
    mrp: Price | None = None
    cost_price: Cost = Decimal(0)
    reorder_threshold: Threshold = Decimal(0)
    barcode: str | None = Field(default=None, pattern=r"^[0-9A-Za-z-]{4,32}$")

    @field_validator("options")
    @classmethod
    def _options(cls, value):
        return check_options(value)

    @model_validator(mode="after")
    def _prices(self):
        if self.mrp is not None and self.mrp < self.selling_price:
            raise ValueError("MRP cannot be lower than the selling price.")
        return self


class VariantPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str | None = Field(default=None, pattern=SKU)
    options: dict[str, str] | None = None
    selling_price: Price | None = None
    mrp: Price | None = None
    cost_price: Cost | None = None
    reorder_threshold: Threshold | None = None
    barcode: str | None = Field(default=None, pattern=r"^[0-9A-Za-z-]{4,32}$")
    is_active: bool | None = None
    archived: bool | None = None

    @field_validator("options")
    @classmethod
    def _options(cls, value):
        return None if value is None else check_options(value)


class RecommendationDiscount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_price: Price
    discount_pct: Annotated[Decimal, Field(gt=0, lt=90, max_digits=5, decimal_places=2)]
    margin_floor: Annotated[Decimal, Field(ge=Decimal("0.20"), lt=1)] = Decimal("0.20")
    application_key: str = Field(min_length=1, max_length=100)


def install_editing_routes(app: FastAPI, *, Ctx, Db, product_detail) -> None:
    def writer(ctx: InternalContext) -> str:
        ctx.require_permission("catalog.write")
        if not ctx.business_id:
            raise ApiError(404, "not_found", "Not found.")
        return ctx.business_id

    def one(db: Session, model, b: str, row_id: str, *, lock=False):
        query = select(model).where(model.business_id == b, model.id == row_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        row = db.scalar(query)
        if row is None:
            raise ApiError(404, "not_found", "Not found.")
        return row

    def unique_slug(db: Session, model, b: str, wanted: str, exclude: str | None = None) -> str:
        taken = set(
            db.scalars(
                select(model.slug).where(
                    model.business_id == b,
                    model.slug.like(f"{wanted}%"),
                    *([model.id != exclude] if exclude else []),
                )
            )
        )
        if wanted not in taken:
            return wanted
        n = 2
        while f"{wanted}-{n}" in taken:
            n += 1
        return f"{wanted}-{n}"

    def category_ref(db: Session, b: str, category_id: str | None) -> None:
        if category_id is None:
            return
        c = db.scalar(select(Category).where(Category.business_id == b, Category.id == category_id))
        if c is None or c.archived_at is not None:
            raise ApiError(
                422, "validation_error", "Unknown category.", {"category_id": "Unknown category."}
            )

    def commit(db: Session, conflict: str, message: str, field: str) -> None:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ApiError(409, conflict, message, {field: message}) from None

    def active_variants(db: Session, product_id: str) -> int:
        return db.scalar(
            select(func.count())
            .select_from(ProductVariant)
            .where(
                ProductVariant.product_id == product_id,
                ProductVariant.is_active.is_(True),
                ProductVariant.archived_at.is_(None),
            )
        )

    def category_out(c: Category) -> dict:
        return {
            "id": c.id,
            "name": c.name,
            "slug": c.slug,
            "parent_id": c.parent_id,
            "position": c.position,
            "archived": c.archived_at is not None,
        }

    # ---- categories ---------------------------------------------------------------------------
    @app.post("/internal/v1/categories", status_code=201)
    def create_category(body: CategoryIn, ctx: Ctx, db: Db):
        b = writer(ctx)
        category_ref(db, b, body.parent_id)
        slug = body.slug or unique_slug(db, Category, b, slugify(body.name))
        c = Category(
            business_id=b,
            name=body.name,
            slug=slug,
            parent_id=body.parent_id,
            position=body.position,
        )
        db.add(c)
        commit(db, "slug_taken", "That web name is already used.", "slug")
        return category_out(c)

    @app.patch("/internal/v1/categories/{category_id}")
    def edit_category(category_id: str, body: CategoryPatch, ctx: Ctx, db: Db):
        b = writer(ctx)
        c = one(db, Category, b, category_id, lock=True)
        if body.name is not None:
            c.name = body.name
        if body.position is not None:
            c.position = body.position
        if body.archived is True and c.archived_at is None:
            in_use = db.scalar(
                select(func.count())
                .select_from(Product)
                .where(
                    Product.business_id == b,
                    Product.category_id == c.id,
                    Product.archived_at.is_(None),
                )
            )
            if in_use:
                raise ApiError(
                    409,
                    "category_in_use",
                    f"{in_use} product(s) still use this category; move them first.",
                )
            c.archived_at = utc_now()
        elif body.archived is False:
            c.archived_at = None
        db.commit()
        return category_out(c)

    # ---- products ------------------------------------------------------------------------------
    @app.post("/internal/v1/products", status_code=201)
    def create_product(body: ProductIn, ctx: Ctx, db: Db):
        b = writer(ctx)
        category_ref(db, b, body.category_id)
        data = body.model_dump()
        data["slug"] = body.slug or unique_slug(db, Product, b, slugify(body.name))
        p = Product(business_id=b, status="DRAFT", **data)
        db.add(p)
        commit(db, "slug_taken", "That web name is already used.", "slug")
        return product_detail(db, ctx, p.id)

    @app.patch("/internal/v1/products/{product_id}")
    def edit_product(product_id: str, body: ProductPatch, ctx: Ctx, db: Db):
        b = writer(ctx)
        p = one(db, Product, b, product_id, lock=True)
        if p.status == "ARCHIVED":
            raise ApiError(409, "archived", "Archived products cannot be edited.")
        changes = body.model_dump(exclude_unset=True)
        if "category_id" in changes:
            category_ref(db, b, changes["category_id"])
        if changes.get("stock_unit") not in (None, p.stock_unit) and p.status != "DRAFT":
            raise ApiError(
                409, "unit_locked", "The stock unit can only change while the product is a draft."
            )
        if p.status == "PUBLISHED" and changes.get("hsn_sac", p.hsn_sac) is None:
            raise ApiError(409, "hsn_required", "Published products need an HSN/SAC code.")
        for key, value in changes.items():
            if value is None and key in ("name", "tax_class", "stock_unit"):
                continue
            if key == "slug" and value:
                value = (
                    value if value == p.slug else unique_slug(db, Product, b, value, exclude=p.id)
                )
            setattr(p, key, value)
        commit(db, "slug_taken", "That web name is already used.", "slug")
        return product_detail(db, ctx, p.id)

    def set_status(db: Session, ctx: InternalContext, product_id: str, status: str) -> dict:
        b = writer(ctx)
        p = one(db, Product, b, product_id, lock=True)
        if p.status == status:
            return product_detail(db, ctx, p.id)
        if p.status == "ARCHIVED":
            raise ApiError(409, "archived", "Archived products cannot change status.")
        if status == "PUBLISHED":
            problems = {}
            if not p.hsn_sac:
                problems["hsn_sac"] = "Add the HSN/SAC code."
            if p.tax_class not in CURRENT_TAX_CLASSES:
                problems["tax_class"] = "Choose a current GST class."
            if not active_variants(db, p.id):
                problems["variants"] = "Add at least one active variant with a price."
            if problems:
                raise ApiError(
                    422, "not_publishable", "This product is not ready to publish.", problems
                )
        p.status = status
        if status == "ARCHIVED":
            p.archived_at = utc_now()
        db.commit()
        return product_detail(db, ctx, p.id)

    @app.post("/internal/v1/products/{product_id}/publish")
    def publish(product_id: str, ctx: Ctx, db: Db):
        return set_status(db, ctx, product_id, "PUBLISHED")

    @app.post("/internal/v1/products/{product_id}/unpublish")
    def unpublish(product_id: str, ctx: Ctx, db: Db):
        return set_status(db, ctx, product_id, "DRAFT")

    @app.post("/internal/v1/products/{product_id}/archive")
    def archive(product_id: str, ctx: Ctx, db: Db):
        return set_status(db, ctx, product_id, "ARCHIVED")

    # ---- variants ------------------------------------------------------------------------------
    @app.post("/internal/v1/products/{product_id}/variants", status_code=201)
    def add_variant(product_id: str, body: VariantIn, ctx: Ctx, db: Db):
        b = writer(ctx)
        p = one(db, Product, b, product_id, lock=True)
        if p.status == "ARCHIVED":
            raise ApiError(409, "archived", "Archived products cannot get new variants.")
        if db.scalar(
            select(ProductVariant.id).where(
                ProductVariant.business_id == b, ProductVariant.sku == body.sku
            )
        ):
            raise ApiError(409, "sku_taken", "That SKU is already used.", {"sku": "Already used."})
        digest = option_hash(body.options)
        if db.scalar(
            select(ProductVariant.id).where(
                ProductVariant.product_id == p.id, ProductVariant.option_hash == digest
            )
        ):
            raise ApiError(
                409,
                "duplicate_options",
                "A variant with these options exists.",
                {"options": "Duplicate."},
            )
        v = ProductVariant(
            business_id=b, product_id=p.id, option_hash=digest, is_active=True, **body.model_dump()
        )
        db.add(v)
        commit(db, "sku_taken", "That SKU or option combination is already used.", "sku")
        return product_detail(db, ctx, p.id)

    @app.post("/internal/v1/variants/{variant_id}/recommendation-discount")
    def apply_recommendation_discount(
        variant_id: str, body: RecommendationDiscount, ctx: Ctx, db: Db
    ):
        b = writer(ctx)
        ctx.require_permission("insights.act")
        if ctx.actor_type != "OWNER":
            raise ApiError(403, "forbidden", "An owner account is required.")
        v = one(db, ProductVariant, b, variant_id, lock=True)
        p = one(db, Product, b, v.product_id, lock=True)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "variant": variant_id,
                    "price": format(body.original_price.normalize(), "f"),
                    "discount": format(body.discount_pct.normalize(), "f"),
                    "margin_floor": format(body.margin_floor.normalize(), "f"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        receipt = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.tenant_key == b,
                IdempotencyKey.scope == "catalog.recommendation-discount",
                IdempotencyKey.key == body.application_key,
            )
        )
        if receipt is not None:
            if (
                receipt.service != "catalog"
                or receipt.request_hash != fingerprint
                or receipt.status != "DONE"
            ):
                raise ApiError(
                    409,
                    "discount_conflict",
                    "This discount application conflicts with an earlier request.",
                )
            return receipt.response_body
        if (
            not v.is_active
            or v.archived_at is not None
            or p.status != "PUBLISHED"
            or p.archived_at is not None
        ):
            raise ApiError(409, "unavailable_variant", "This variant is no longer on sale.")
        price = (body.original_price * (1 - body.discount_pct / 100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if price <= 0 or price >= body.original_price:
            raise ApiError(
                409, "invalid_discount", "The discount must reduce the price to a positive amount."
            )
        if (price - v.cost_price) / price < body.margin_floor:
            raise ApiError(
                409,
                "margin_floor",
                "The latest cost makes this discount unsafe. Review the price first.",
            )
        # Only a committed receipt proves a retry. An already-matching price alone proves nothing.
        if v.selling_price != body.original_price:
            raise ApiError(
                409,
                "price_changed",
                "The price changed since this recommendation. Review it before applying.",
            )
        result = {
            "variant_id": v.id,
            "original_price": str(body.original_price),
            "selling_price": str(price),
            "discount_pct": str(body.discount_pct),
        }
        v.selling_price = price
        db.add(
            IdempotencyKey(
                service="catalog",
                tenant_key=b,
                scope="catalog.recommendation-discount",
                key=body.application_key,
                request_hash=fingerprint,
                status="DONE",
                response_code=200,
                response_body=result,
                expires_at=utc_now() + timedelta(days=3650),
            )
        )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ApiError(
                409,
                "discount_conflict",
                "The discount was updated concurrently. Retry the request.",
            ) from exc
        return result

    @app.patch("/internal/v1/variants/{variant_id}")
    def edit_variant(variant_id: str, body: VariantPatch, ctx: Ctx, db: Db):
        b = writer(ctx)
        v = one(db, ProductVariant, b, variant_id, lock=True)
        p = one(db, Product, b, v.product_id, lock=True)
        changes = body.model_dump(exclude_unset=True)
        archived = changes.pop("archived", None)
        if v.archived_at is not None and archived is not False:
            raise ApiError(409, "archived", "Archived variants cannot be edited.")
        price = changes.get("selling_price") or v.selling_price
        mrp = changes["mrp"] if "mrp" in changes else v.mrp
        if mrp is not None and mrp < price:
            raise ApiError(
                422,
                "validation_error",
                "MRP cannot be lower than the selling price.",
                {"mrp": "Lower than the price."},
            )
        if "sku" in changes and changes["sku"] and changes["sku"] != v.sku:
            if db.scalar(
                select(ProductVariant.id).where(
                    ProductVariant.business_id == b, ProductVariant.sku == changes["sku"]
                )
            ):
                raise ApiError(
                    409, "sku_taken", "That SKU is already used.", {"sku": "Already used."}
                )
        if changes.get("options") is not None:
            digest = option_hash(changes["options"])
            clash = db.scalar(
                select(ProductVariant.id).where(
                    ProductVariant.product_id == p.id,
                    ProductVariant.option_hash == digest,
                    ProductVariant.id != v.id,
                )
            )
            if clash:
                raise ApiError(409, "duplicate_options", "A variant with these options exists.")
            v.option_hash = digest
        going_away = changes.get("is_active") is False or archived is True
        if (
            going_away
            and p.status == "PUBLISHED"
            and v.is_active
            and v.archived_at is None
            and active_variants(db, p.id) <= 1
        ):
            raise ApiError(
                409,
                "last_variant",
                "This is the last variant on sale; unpublish the product first.",
            )
        for key, value in changes.items():
            if value is None and key in (
                "sku",
                "options",
                "selling_price",
                "cost_price",
                "reorder_threshold",
                "is_active",
            ):
                continue
            setattr(v, key, value)
        if archived is True:
            v.archived_at, v.is_active = utc_now(), False
        elif archived is False:
            v.archived_at = None
        commit(db, "sku_taken", "That SKU is already used.", "sku")
        return product_detail(db, ctx, p.id)
