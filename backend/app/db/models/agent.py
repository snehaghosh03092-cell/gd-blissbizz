"""OWNER SERVICE: agent. Conversations, audit of tool calls, voice logs, recommendations.

Transcripts only, never raw audio. The agent service reads business data through ``rpt_*`` views
with a
read-only database user; it writes only these tables.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models._common import (
    BIGPK,
    COST,
    MONEY,
    QTY,
    business_fk,
    enum_check,
    id_column,
    utc_now,
)
from app.db.session import Base

REC_TYPES = ("REORDER", "DISCOUNT", "DEAD_STOCK", "REVIEW")
REC_STATUSES = ("OPEN", "ACCEPTED", "DISMISSED", "POSTPONED", "RESOLVED")


class AgentConversation(Base):
    __tablename__ = "agent_conversations"
    __table_args__ = (
        enum_check("audience", ("ONBOARDING", "SHOPPER", "OWNER"), "audience"),
        Index("ix_agent_conversations_business_created", "business_id", "created_at"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str | None] = business_fk(nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(36))
    audience: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        enum_check("role", ("USER", "ASSISTANT"), "role"),
        Index("ix_agent_messages_conversation", "conversation_id"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text())
    route: Mapped[str | None] = mapped_column(String(30))
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    prompt_tokens: Mapped[int] = mapped_column(Integer(), default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer(), default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer(), default=0)
    latency_ms: Mapped[int] = mapped_column(Integer(), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class AgentToolCall(Base):
    """Audit of every tool execution. Arguments are hashed, not stored, to avoid keeping personal
    data."""

    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        enum_check("status", ("OK", "ERROR", "DENIED"), "status"),
        Index("ix_agent_tool_calls_business_created", "business_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    business_id: Mapped[str | None] = business_fk(nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_conversations.id")
    )
    message_id: Mapped[int | None] = mapped_column(BIGPK)
    tool_name: Mapped[str] = mapped_column(String(80))
    args_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(8))
    duration_ms: Mapped[int] = mapped_column(Integer(), default=0)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class VoiceLog(Base):
    """Transcripts, route, token counts and latency only. Never raw audio."""

    __tablename__ = "voice_logs"
    __table_args__ = (Index("ix_voice_logs_business_created", "business_id", "created_at"),)

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    business_id: Mapped[str] = business_fk()
    role: Mapped[str] = mapped_column(String(20))
    transcript: Mapped[str] = mapped_column(Text())
    route: Mapped[str] = mapped_column(String(30))
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    prompt_tokens: Mapped[int] = mapped_column(Integer(), default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer(), default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer(), default=0)
    latency_ms: Mapped[int] = mapped_column(Integer(), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class AnalysisRun(Base):
    """One row per (business, day, ruleset); the unique run_key makes a run idempotent."""

    __tablename__ = "analysis_runs"
    __table_args__ = (
        UniqueConstraint("run_key", name="uq_analysis_runs_run_key"),
        Index("ix_analysis_runs_business_started", "business_id", "started_at"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    run_key: Mapped[str] = mapped_column(String(80))
    as_of: Mapped[date] = mapped_column(Date())
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="running")
    products_analyzed: Mapped[int] = mapped_column(Integer(), default=0)
    created_count: Mapped[int] = mapped_column(Integer(), default=0)
    updated_count: Mapped[int] = mapped_column(Integer(), default=0)
    resolved_count: Mapped[int] = mapped_column(Integer(), default=0)
    error: Mapped[str | None] = mapped_column(Text())
    started_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime())


class Recommendation(Base):
    """OPEN -> ACCEPTED | DISMISSED (cooldown) | POSTPONED (until snooze_until) -> RESOLVED."""

    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint("business_id", "dedup_key", name="uq_recommendations_business_dedup"),
        enum_check("rec_type", REC_TYPES, "rec_type"),
        enum_check("status", REC_STATUSES, "status"),
        enum_check("severity", ("critical", "high", "medium", "low"), "severity"),
        Index("ix_recommendations_business_status_severity", "business_id", "status", "severity"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    variant_id: Mapped[str | None] = mapped_column(String(36))
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("analysis_runs.id"))
    rec_type: Mapped[str] = mapped_column(String(12))
    severity: Mapped[str] = mapped_column(String(10))
    confidence: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), default="OPEN")
    dedup_key: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text())
    action: Mapped[str] = mapped_column(Text())
    suggested_qty: Mapped[Decimal | None] = mapped_column(QTY)
    suggested_discount_pct: Mapped[Decimal | None] = mapped_column(MONEY)
    value_at_risk: Mapped[Decimal | None] = mapped_column(COST)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    snooze_until: Mapped[date | None] = mapped_column(Date())
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class RecommendationEvent(Base):
    __tablename__ = "recommendation_events"
    __table_args__ = (Index("ix_recommendation_events_recommendation", "recommendation_id"),)

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    business_id: Mapped[str] = business_fk()
    recommendation_id: Mapped[str] = mapped_column(String(36), ForeignKey("recommendations.id"))
    user_id: Mapped[str | None] = mapped_column(String(36))
    from_status: Mapped[str] = mapped_column(String(10))
    to_status: Mapped[str] = mapped_column(String(10))
    comment: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
