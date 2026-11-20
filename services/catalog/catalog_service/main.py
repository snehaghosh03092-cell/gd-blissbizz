"""Catalog service: categories, products, variants. The only reader/writer of the catalog tables.

Every query is filtered by the business in the *signed context*; no id from the client selects
a tenant. Cost prices are visible only to callers holding ``catalog.write``.
"""

from typing import Annotated

from app.db.models.catalog import Category, Product, ProductVariant
from app.db.session import get_db
from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError, register_exception_handlers
from blissbiz_common.fastapi import make_internal_dependency
from blissbiz_common.logging import configure_logging, install_request_id
from blissbiz_common.paging import like_pattern, page_query
from fastapi import Depends, FastAPI, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from catalog_service.editing import install_editing_routes
from catalog_service.public import install_public_routes

STATUSES = ("DRAFT", "PUBLISHED", "ARCHIVED")


class CatalogSettings(ServiceSettings):
    service_name: str = "catalog"


def _business(ctx: InternalContext) -> str:
    if ctx.business_id is None:
        raise ApiError(404, "not_found", "Not found.")
    return ctx.business_id


def _money(value) -> str | None:
    return None if value is None else f"{value:.2f}"


def create_app(settings: CatalogSettings | None = None, configure_logs: bool = False) -> FastAPI:
    settings = settings or CatalogSettings()
    if configure_logs:
        configure_logging(settings.service_name, settings.log_level)
    internal = make_internal_dependency(settings)
    app = FastAPI(
        title="BlissBiz catalog", docs_url=None, redoc_url=None, openapi_url="/openapi.json"
    )
    install_request_id(app)
    register_exception_handlers(app)
    Ctx = Annotated[InternalContext, Depends(internal)]
    Db = Annotated[Session, Depends(get_db)]
    paging = Depends(page_query())
    paging_wide = Depends(
        page_query(default_size=100, max_size=200)
    )  # used by gateway compositions

    @app.get("/health")
    def health():
        return {"status": "ok", "service": settings.service_name}

    @app.get("/internal/v1/health")
    def ready(ctx: Ctx, db: Db):
        db.execute(text("SELECT 1"))
        return {"status": "ready", "service": settings.service_name}

    @app.get("/internal/v1/categories")
    def categories(ctx: Ctx, db: Db):
        ctx.require_permission("catalog.read")
        b = _business(ctx)
        rows = db.execute(
            select(Category, func.count(Product.id))
            .outerjoin(
                Product,
                (Product.category_id == Category.id)
                & (Product.business_id == b)
                & (Product.status != "ARCHIVED"),
            )
            .where(Category.business_id == b, Category.archived_at.is_(None))
            .group_by(Category.id)
            .order_by(Category.position, Category.name)
        ).all()
        return {
            "items": [{"id": c.id, "name": c.name, "slug": c.slug, "products": n} for c, n in rows]
        }

    @app.get("/internal/v1/products")
    def products(
        ctx: Ctx,
        db: Db,
        page_args: Annotated[tuple[int, int], paging],
        status: Annotated[str | None, Query()] = None,
        category: Annotated[str | None, Query(max_length=100)] = None,
        q: Annotated[str | None, Query(max_length=100)] = None,
    ):
        ctx.require_permission("catalog.read")
        b = _business(ctx)
        if status is not None and status not in STATUSES:
            raise ApiError(
                422, "validation_error", "Invalid status.", {"status": f"one of {STATUSES}"}
            )
        page, size = page_args
        stats = (
            select(
                ProductVariant.product_id.label("pid"),
                func.count(ProductVariant.id).label("variants"),
                func.min(ProductVariant.selling_price).label("price_min"),
                func.max(ProductVariant.selling_price).label("price_max"),
            )
            .where(ProductVariant.business_id == b, ProductVariant.archived_at.is_(None))
            .group_by(ProductVariant.product_id)
            .subquery()
        )
        base = (
            select(Product, Category.name, stats.c.variants, stats.c.price_min, stats.c.price_max)
            .outerjoin(Category, Category.id == Product.category_id)
            .outerjoin(stats, stats.c.pid == Product.id)
            .where(Product.business_id == b)
        )
        if status:
            base = base.where(Product.status == status)
        if category:
            base = base.where(Category.slug == category)
        if q:
            base = base.where(Product.name.ilike(like_pattern(q), escape="\\"))
        total = db.scalar(select(func.count()).select_from(base.subquery()))
        rows = db.execute(base.order_by(Product.name).limit(size).offset((page - 1) * size)).all()
        return {
            "items": [
                {
                    "id": p.id,
                    "name": p.name,
                    "slug": p.slug,
                    "status": p.status,
                    "brand": p.brand,
                    "category": cat,
                    "stock_unit": p.stock_unit,
                    "tax_class": p.tax_class,
                    "hsn_sac": p.hsn_sac,
                    "variants": variants or 0,
                    "price_min": _money(pmin),
                    "price_max": _money(pmax),
                }
                for p, cat, variants, pmin, pmax in rows
            ],
            "page": page,
            "page_size": size,
            "total": total,
        }

    def product_detail(db: Session, ctx: InternalContext, product_id: str) -> dict:
        b = _business(ctx)
        p = db.scalar(select(Product).where(Product.id == product_id, Product.business_id == b))
        if p is None:
            raise ApiError(
                404, "not_found", "Not found."
            )  # also what another tenant's id looks like
        category = db.get(Category, p.category_id) if p.category_id else None
        variants = db.scalars(
            select(ProductVariant)
            .where(ProductVariant.product_id == p.id, ProductVariant.business_id == b)
            .order_by(ProductVariant.sku)
        ).all()
        can_see_cost = ctx.has("catalog.write")
        return {
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "description": p.description,
            "brand": p.brand,
            "status": p.status,
            "category": category.name if category else None,
            "category_id": p.category_id,
            "stock_unit": p.stock_unit,
            "tax_class": p.tax_class,
            "hsn_sac": p.hsn_sac,
            "archived": p.archived_at is not None,
            "variants": [
                {
                    "id": v.id,
                    "sku": v.sku,
                    "options": v.options,
                    "price": _money(v.selling_price),
                    "mrp": _money(v.mrp),
                    "reorder_threshold": str(v.reorder_threshold),
                    "is_active": v.is_active,
                    "archived": v.archived_at is not None,
                    "barcode": v.barcode,
                    **({"cost_price": _money(v.cost_price)} if can_see_cost else {}),
                }
                for v in variants
            ],
        }

    @app.get("/internal/v1/products/{product_id}")
    def product(product_id: str, ctx: Ctx, db: Db):
        ctx.require_permission("catalog.read")
        return product_detail(db, ctx, product_id)

    @app.get("/internal/v1/variants")
    def variants(
        ctx: Ctx,
        db: Db,
        page_args: Annotated[tuple[int, int], paging_wide],
        q: Annotated[str | None, Query(max_length=100)] = None,
    ):
        """Flat variant list (with product context); the gateway joins it with stock from supply."""
        ctx.require_permission("catalog.read")
        b = _business(ctx)
        page, size = page_args
        base = (
            select(ProductVariant, Product, Category.name)
            .join(Product, Product.id == ProductVariant.product_id)
            .outerjoin(Category, Category.id == Product.category_id)
            .where(ProductVariant.business_id == b, ProductVariant.archived_at.is_(None))
        )
        if q:
            pattern = like_pattern(q)
            base = base.where(
                Product.name.ilike(pattern, escape="\\")
                | ProductVariant.sku.ilike(pattern, escape="\\")
            )
        total = db.scalar(select(func.count()).select_from(base.subquery()))
        rows = db.execute(
            base.order_by(Product.name, ProductVariant.sku).limit(size).offset((page - 1) * size)
        ).all()
        return {
            "items": [
                {
                    "variant_id": v.id,
                    "product_id": p.id,
                    "product_name": p.name,
                    "sku": v.sku,
                    "options": v.options,
                    "category": cat,
                    "product_status": p.status,
                    "is_active": v.is_active,
                    "price": _money(v.selling_price),
                    "reorder_threshold": str(v.reorder_threshold),
                    "stock_unit": p.stock_unit,
                }
                for v, p, cat in rows
            ],
            "page": page,
            "page_size": size,
            "total": total,
        }

    install_public_routes(app, Ctx=Ctx, Db=Db)
    install_editing_routes(app, Ctx=Ctx, Db=Db, product_detail=product_detail)
    return app


app = create_app(configure_logs=True)
