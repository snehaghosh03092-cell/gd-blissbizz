"""SHARED PLATFORM TABLES: written by every service; each row is stamped with ``service``.

audit_events, idempotency_keys, jobs, outbox_events, processed_events, error_events,
integration_configs.
These are infrastructure shared by design (docs/02 section 2); tenant references are plain columns
except
``business_id`` foreign keys to the tenant anchor.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models._common import BIGPK, business_fk, enum_check, id_column, utc_now
from app.db.session import Base

SERVICES = (
    "gateway",
    "identity",
    "store",
    "catalog",
    "supply",
    "sales",
    "billing",
    "agent",
    "notification",
)


class AuditEvent(Base):
    """Append-only (enforced by trigger)."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_business_created", "business_id", "created_at"),
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(30))
    business_id: Mapped[str | None] = business_fk(nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("tenant_key", "scope", "key", name="uq_idempotency_keys_tenant_scope_key"),
        enum_check("status", ("IN_PROGRESS", "DONE"), "status"),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    id: Mapped[str] = id_column()
    service: Mapped[str] = mapped_column(String(30))
    # business id, or 'global' for pre-tenant operations (no FK: it may hold a sentinel)
    tenant_key: Mapped[str] = mapped_column(String(36))
    scope: Mapped[str] = mapped_column(String(60))
    key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), default="IN_PROGRESS")
    response_code: Mapped[int | None] = mapped_column(Integer())
    response_body: Mapped[dict | list | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class Job(Base):
    """Per-service queues (``queue = <service>``) claimed with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("type", "dedupe_key", name="uq_jobs_type_dedupe_key"),
        enum_check("status", ("QUEUED", "RUNNING", "DONE", "FAILED", "DEAD"), "status"),
        Index("ix_jobs_queue_status_run_at", "queue", "status", "run_at"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    queue: Mapped[str] = mapped_column(String(30))
    type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="QUEUED")
    run_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    attempts: Mapped[int] = mapped_column(Integer(), default=0)
    max_attempts: Mapped[int] = mapped_column(Integer(), default=5)
    locked_by: Mapped[str | None] = mapped_column(String(80))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime())
    last_error: Mapped[str | None] = mapped_column(Text())
    dedupe_key: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class OutboxEvent(Base):
    """Transactional outbox: written in the same transaction as the state change, relayed by
    HTTP."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_outbox_events_event_id"),
        enum_check("status", ("PENDING", "DELIVERED", "FAILED"), "status"),
        Index("ix_outbox_events_service_status_next", "service", "status", "next_attempt_at"),
        Index("ix_outbox_events_business_created", "business_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36))
    service: Mapped[str] = mapped_column(String(30))
    event_type: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer(), default=1)
    business_id: Mapped[str | None] = mapped_column(String(36))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer(), default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime())


class ProcessedEvent(Base):
    """Consumer-side dedupe so event handlers are idempotent."""

    __tablename__ = "processed_events"

    consumer: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class ErrorEvent(Base):
    __tablename__ = "error_events"
    __table_args__ = (Index("ix_error_events_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(30))
    business_id: Mapped[str | None] = business_fk(nullable=True)
    source: Mapped[str] = mapped_column(String(80))
    code: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(String(500))
    context: Mapped[dict | None] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime())


class IntegrationConfig(Base):
    """Non-secret settings plus a reference to a secret (env var name); secrets are never stored
    in clear."""

    __tablename__ = "integration_configs"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "provider_kind", "provider", name="uq_integration_configs_scope_provider"
        ),
        enum_check(
            "provider_kind",
            ("PAYMENT", "EMAIL", "SMS", "DEPLOY", "DOMAIN", "GST", "LLM", "STT"),
            "provider_kind",
        ),
        enum_check("status", ("ENABLED", "DISABLED"), "status"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str | None] = business_fk(nullable=True)
    provider_kind: Mapped[str] = mapped_column(String(10))
    provider: Mapped[str] = mapped_column(String(40))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_ref: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(10), default="DISABLED")
    updated_by: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


__all__ = [
    "AuditEvent",
    "ErrorEvent",
    "IdempotencyKey",
    "IntegrationConfig",
    "Job",
    "OutboxEvent",
    "ProcessedEvent",
    "SERVICES",
]
