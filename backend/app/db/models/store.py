"""OWNER SERVICE: store. Storefront configuration, policies, host -> business mapping.

Deployment tables exist so the schema is complete, but deployment features are deferred (local
testing only).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models._common import BIGPK, MONEY, business_fk, enum_check, id_column, utc_now
from app.db.session import Base

TEMPLATE_KEYS = ("fashion", "grocery", "electronics", "gaming", "food", "beauty", "craft", "home")


class Storefront(Base):
    __tablename__ = "storefronts"
    __table_args__ = (
        enum_check("status", ("DRAFT", "PREVIEW", "PUBLISHED", "UNPUBLISHED"), "status"),
    )

    business_id: Mapped[str] = business_fk(primary_key=True)
    template_key: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(12), default="DRAFT")
    theme: Mapped[dict] = mapped_column(JSON, default=dict)
    draft_config: Mapped[dict] = mapped_column(JSON, default=dict)
    published_config: Mapped[dict | None] = mapped_column(JSON)
    config_version: Mapped[int] = mapped_column(Integer(), default=1)
    published_at: Mapped[datetime | None] = mapped_column(DateTime())
    unpublished_at: Mapped[datetime | None] = mapped_column(DateTime())
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class StorePolicy(Base):
    """Typed, computable policy facts (answered with zero LLM calls). Ported from the voice
    agent."""

    __tablename__ = "store_policies"

    business_id: Mapped[str] = business_fk(primary_key=True)
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata")
    support_hours: Mapped[dict | None] = mapped_column(
        JSON
    )  # {"mon": ["09:00","18:00"], "sun": null}
    support_phone: Mapped[str | None] = mapped_column(String(40))
    support_email: Mapped[str | None] = mapped_column(String(254))
    refund_window_days: Mapped[int | None] = mapped_column(Integer())
    refund_method: Mapped[str | None] = mapped_column(String(190))
    return_window_days: Mapped[int | None] = mapped_column(Integer())
    return_conditions: Mapped[str | None] = mapped_column(Text())
    shipping_days_min: Mapped[int | None] = mapped_column(Integer())
    shipping_days_max: Mapped[int | None] = mapped_column(Integer())
    free_shipping_over: Mapped[Decimal | None] = mapped_column(MONEY)
    flat_shipping_fee: Mapped[Decimal | None] = mapped_column(MONEY)
    warranty_days: Mapped[int | None] = mapped_column(Integer())
    cancel_window_hours: Mapped[int | None] = mapped_column(Integer())
    show_exact_stock: Mapped[bool] = mapped_column(Boolean(), default=False)
    version: Mapped[int] = mapped_column(Integer(), default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class PolicyDocument(Base):
    __tablename__ = "policy_documents"
    __table_args__ = (
        UniqueConstraint("business_id", "slug", name="uq_policy_documents_business_slug"),
        enum_check("audience", ("PUBLIC", "OWNER"), "audience"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    slug: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(160))
    audience: Mapped[str] = mapped_column(String(10), default="PUBLIC")
    body: Mapped[str] = mapped_column(Text())
    version: Mapped[int] = mapped_column(Integer(), default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class Domain(Base):
    """Host -> business. The gateway resolves storefront tenants only from ACTIVE rows."""

    __tablename__ = "domains"
    __table_args__ = (
        UniqueConstraint("hostname", name="uq_domains_hostname"),
        enum_check("kind", ("SUBDOMAIN", "CUSTOM"), "kind"),
        enum_check("status", ("PENDING", "VERIFYING", "ACTIVE", "FAILED", "REMOVED"), "status"),
        enum_check("tls_status", ("NONE", "PENDING", "ACTIVE", "FAILED"), "tls_status"),
        Index("ix_domains_business_id", "business_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    hostname: Mapped[str] = mapped_column(String(253))
    kind: Mapped[str] = mapped_column(String(10), default="SUBDOMAIN")
    status: Mapped[str] = mapped_column(String(10), default="PENDING")
    tls_status: Mapped[str] = mapped_column(String(10), default="NONE")
    verification: Mapped[dict | None] = mapped_column(JSON)
    provider: Mapped[str | None] = mapped_column(String(40))
    provider_ref: Mapped[str | None] = mapped_column(String(120))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime())
    error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class Deployment(Base):
    """Deferred feature: table exists, no rows are produced yet."""

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_deployments_business_key"),
        enum_check(
            "status",
            (
                "QUEUED",
                "BUILDING",
                "DEPLOYING",
                "HEALTH_CHECKING",
                "SUCCEEDED",
                "FAILED",
                "ROLLED_BACK",
            ),
            "status",
        ),
        Index("ix_deployments_business_started", "business_id", "started_at"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    provider: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")
    config_version: Mapped[int] = mapped_column(Integer())
    attempt: Mapped[int] = mapped_column(Integer(), default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime())
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(String(500))
    requested_by: Mapped[str | None] = mapped_column(String(36))
    idempotency_key: Mapped[str | None] = mapped_column(String(100))


class DeploymentLog(Base):
    __tablename__ = "deployment_logs"
    __table_args__ = (Index("ix_deployment_logs_deployment_seq", "deployment_id", "seq"),)

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("deployments.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer())
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class HealthCheck(Base):
    __tablename__ = "health_checks"

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("deployments.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(500))
    status_code: Mapped[int | None] = mapped_column(Integer())
    ok: Mapped[bool] = mapped_column(Boolean(), default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer())
    checked_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
