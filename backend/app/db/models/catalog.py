"""OWNER SERVICE: catalog. Categories, products, variants (the unit of stock and sale), images,
imports.

Other services refer to ``variant_id``/``product_id`` as plain columns (no foreign keys) and
snapshot what
they need (name, sku, price, HSN) at write time.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models._common import COST, MONEY, QTY, business_fk, enum_check, id_column, utc_now
from app.db.session import Base

PRODUCT_STATUSES = ("DRAFT", "PUBLISHED", "ARCHIVED")
# GST_12 and GST_28 are historical classes kept so old invoices stay explainable (see
# tax_rate_versions).
TAX_CLASSES = (
    "GST_0",
    "GST_5",
    "GST_12",
    "GST_18",
    "GST_28",
    "GST_40",
    "EXEMPT",
    "NONE",
)
STOCK_UNITS = ("PCS", "KG", "G", "L", "ML", "PACK", "BOX", "DOZEN", "PAIR")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("business_id", "slug", name="uq_categories_business_slug"),)

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    parent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("categories.id"))
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer(), default=0)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime())


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("business_id", "slug", name="uq_products_business_slug"),
        enum_check("status", PRODUCT_STATUSES, "status"),
        enum_check("tax_class", TAX_CLASSES, "tax_class"),
        Index("ix_products_business_status_category", "business_id", "status", "category_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    category_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("categories.id"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text())
    brand: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10), default="DRAFT")
    hsn_sac: Mapped[str | None] = mapped_column(String(8))
    tax_class: Mapped[str] = mapped_column(String(10), default="NONE")
    stock_unit: Mapped[str] = mapped_column(String(10), default="PCS")
    track_inventory: Mapped[bool] = mapped_column(Boolean(), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime())


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("business_id", "sku", name="uq_product_variants_business_sku"),
        UniqueConstraint("product_id", "option_hash", name="uq_product_variants_product_options"),
        CheckConstraint(
            "selling_price >= 0 AND (mrp IS NULL OR mrp >= selling_price)", name="price_sane"
        ),
        CheckConstraint("cost_price >= 0", name="cost_non_negative"),
        Index("ix_product_variants_business_product", "business_id", "product_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    product_id: Mapped[str] = mapped_column(String(36), ForeignKey("products.id"))
    sku: Mapped[str] = mapped_column(String(60))
    options: Mapped[dict] = mapped_column(JSON, default=dict)  # {"size": "M", "color": "Black"}
    option_hash: Mapped[str] = mapped_column(String(64))
    barcode: Mapped[str | None] = mapped_column(String(32))
    cost_price: Mapped[Decimal] = mapped_column(COST, default=0)
    selling_price: Mapped[Decimal] = mapped_column(MONEY)
    mrp: Mapped[Decimal | None] = mapped_column(MONEY)
    reorder_threshold: Mapped[Decimal] = mapped_column(QTY, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean(), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime())


class ProductImage(Base):
    __tablename__ = "product_images"
    __table_args__ = (Index("ix_product_images_business_product", "business_id", "product_id"),)

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    product_id: Mapped[str] = mapped_column(String(36), ForeignKey("products.id"))
    variant_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("product_variants.id"))
    storage_path: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(40))
    byte_size: Mapped[int] = mapped_column(Integer())
    sha256: Mapped[str] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer())
    height: Mapped[int | None] = mapped_column(Integer())
    position: Mapped[int] = mapped_column(Integer(), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class CatalogImport(Base):
    """Dry-run creates a VALIDATED row; commit applies it exactly once."""

    __tablename__ = "catalog_imports"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_catalog_imports_business_key"),
        enum_check("status", ("VALIDATED", "COMMITTED", "FAILED", "EXPIRED"), "status"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    status: Mapped[str] = mapped_column(String(10), default="VALIDATED")
    source_path: Mapped[str | None] = mapped_column(String(512))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer(), default=0)
    error_count: Mapped[int] = mapped_column(Integer(), default=0)
    row_report: Mapped[dict | list | None] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(36))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime())
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
