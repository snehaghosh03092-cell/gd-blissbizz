"""Shopper-facing catalog reads (any signed caller with a business in its context).

Only PUBLISHED products and active variants are ever returned, and never cost prices. Search
works on words: every word the shopper typed must match the product (name, brand, category) or
the variant's options, so "black tee" finds only the black variants of tees.
"""

import re
from decimal import Decimal
from typing import Annotated

from app.db.models.catalog import Category, Product, ProductVariant
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

MAX_PRODUCTS = 500
STOPWORDS = {
    "a",
    "an",
    "the",
    "for",
    "with",
    "and",
    "in",
    "of",
    "me",
    "show",
    "i",
    "want",
    "some",
    "any",
    "please",
    "find",
    "looking",
    "need",
    "color",
    "colour",
    "size",
}
WORD = re.compile(r"[a-z0-9]+")
PRODUCT_TERM_GROUPS = (
    frozenset({"top", "tee", "tshirt", "shirt", "blouse", "kurta"}),
    frozenset({"bottom", "pant", "trouser", "jean", "short", "skirt"}),
    frozenset({"shoe", "sneaker", "footwear", "sandal", "slipper", "boot"}),
)


def stem(word: str) -> str:
    """Tiny plural folding: dresses->dress, berries->berry, shirts->shirt (not 'glass')."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("es") and word[:-2].endswith(("s", "x", "ch", "sh")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(*texts) -> set[str]:
    out: set[str] = set()
    for t in texts:
        if t:
            out |= {stem(w) for w in WORD.findall(str(t).lower())}
    return out


def query_words(q: str | None) -> set[str]:
    return {w for w in words(q) if w not in STOPWORDS and len(w) > 1} if q else set()


def product_term_matches(term: str, haystack: set[str]) -> bool:
    if term in haystack:
        return True
    return any(term in group and bool(group & haystack) for group in PRODUCT_TERM_GROUPS)


def option(options: dict | None, *keys: str) -> str | None:
    for key in keys:
        value = (options or {}).get(key)
        if value is not None:
            return str(value).strip().lower()
    return None


def money(value) -> str | None:
    return None if value is None else f"{value:.2f}"


class QuoteIn(BaseModel):
    variant_ids: list[str] = Field(min_length=1, max_length=100)


def variant_out(v: ProductVariant) -> dict:
    return {
        "id": v.id,
        "sku": v.sku,
        "options": v.options or {},
        "price": money(v.selling_price),
        "mrp": money(v.mrp),
    }


def install_public_routes(app: FastAPI, *, Ctx, Db) -> None:
    def business(ctx: InternalContext) -> str:
        if ctx.business_id is None:
            raise ApiError(404, "not_found", "Not found.")
        return ctx.business_id

    def published(db: Session, b: str):
        return (
            select(ProductVariant, Product, Category)
            .join(Product, Product.id == ProductVariant.product_id)
            .outerjoin(Category, Category.id == Product.category_id)
            .where(
                ProductVariant.business_id == b,
                Product.business_id == b,
                Product.status == "PUBLISHED",
                Product.archived_at.is_(None),
                ProductVariant.is_active.is_(True),
                ProductVariant.archived_at.is_(None),
            )
        )

    @app.get("/internal/v1/public/categories")
    def categories(ctx: Ctx, db: Db):
        b = business(ctx)
        counts: dict[str, set] = {}
        for _, p, c in db.execute(published(db, b)).all():
            if c is not None:
                counts.setdefault(c.id, set()).add(p.id)
        rows = db.scalars(
            select(Category)
            .where(Category.business_id == b, Category.archived_at.is_(None))
            .order_by(Category.position, Category.name)
        ).all()
        return {
            "items": [
                {"name": c.name, "slug": c.slug, "products": len(counts[c.id])}
                for c in rows
                if c.id in counts
            ]
        }

    @app.get("/internal/v1/public/products")
    def products(
        ctx: Ctx,
        db: Db,
        q: Annotated[str | None, Query(max_length=100)] = None,
        category: Annotated[str | None, Query(max_length=100)] = None,
        color: Annotated[str | None, Query(max_length=40)] = None,
        size: Annotated[str | None, Query(max_length=40)] = None,
        min_price: Annotated[Decimal | None, Query(ge=0, le=10_000_000)] = None,
        max_price: Annotated[Decimal | None, Query(ge=0, le=10_000_000)] = None,
    ):
        """Every matching published product with only its matching variants (the gateway adds
        stock, sorts and pages)."""
        b = business(ctx)
        wanted = query_words(q)
        found: dict[str, dict] = {}
        for v, p, c in db.execute(published(db, b).order_by(Product.name, ProductVariant.sku)):
            if category and (c is None or c.slug != category):
                continue
            if color and option(v.options, "color", "colour") != color.strip().lower():
                continue
            if size and option(v.options, "size") != size.strip().lower():
                continue
            if min_price is not None and v.selling_price < min_price:
                continue
            if max_price is not None and v.selling_price > max_price:
                continue
            if wanted:
                haystack = words(
                    p.name,
                    p.brand,
                    c.name if c else None,
                    *(v.options or {}).values(),
                )
                if not all(product_term_matches(term, haystack) for term in wanted):
                    continue
            entry = found.get(p.id)
            if entry is None:
                if len(found) >= MAX_PRODUCTS:
                    continue
                entry = found[p.id] = {
                    "id": p.id,
                    "name": p.name,
                    "slug": p.slug,
                    "brand": p.brand,
                    "category": c.name if c else None,
                    "category_slug": c.slug if c else None,
                    "stock_unit": p.stock_unit,
                    "created_at": p.created_at.isoformat(),
                    "variants": [],
                }
            entry["variants"].append(variant_out(v))
        return {"items": list(found.values()), "truncated": len(found) >= MAX_PRODUCTS}

    @app.get("/internal/v1/public/products/{slug}")
    def product(slug: str, ctx: Ctx, db: Db):
        b = business(ctx)
        rows = db.execute(
            published(db, b).where(Product.slug == slug).order_by(ProductVariant.sku)
        ).all()
        if not rows:
            raise ApiError(404, "not_found", "Not found.")  # drafts look exactly like missing
        _, p, c = rows[0]
        return {
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "brand": p.brand,
            "description": p.description,
            "category": c.name if c else None,
            "category_slug": c.slug if c else None,
            "stock_unit": p.stock_unit,
            "variants": [variant_out(v) for v, _, _ in rows],
        }

    @app.post("/internal/v1/public/quote")
    def quote(body: QuoteIn, ctx: Ctx, db: Db):
        """Current price and tax facts for variants (used by cart and checkout; never trusts a
        price sent by a browser). Unknown or other-tenant ids are simply absent."""
        b = business(ctx)
        ids = list(dict.fromkeys(body.variant_ids))
        rows = db.execute(
            select(ProductVariant, Product)
            .join(Product, Product.id == ProductVariant.product_id)
            .where(ProductVariant.business_id == b, ProductVariant.id.in_(ids))
        ).all()
        items = {}
        for v, p in rows:
            items[v.id] = {
                "variant_id": v.id,
                "product_id": p.id,
                "product_name": p.name,
                "product_slug": p.slug,
                "sku": v.sku,
                "options": v.options or {},
                "price": money(v.selling_price),
                "mrp": money(v.mrp),
                "tax_class": p.tax_class,
                "hsn_sac": p.hsn_sac,
                "stock_unit": p.stock_unit,
                "sellable": p.status == "PUBLISHED"
                and p.archived_at is None
                and v.is_active
                and v.archived_at is None,
                "archived": p.archived_at is not None or v.archived_at is not None,
            }
            if ctx.actor_type == "SERVICE":  # e.g. supply valuing opening stock; never shoppers
                items[v.id]["cost_price"] = f"{v.cost_price:.4f}"
        return {"items": items}
