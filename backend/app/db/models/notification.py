"""OWNER SERVICE: notification. In-app notifications, templates, and delivery attempts (email/SMS).

Email goes through a GoDaddy mailbox over SMTP when configured; SMS has no GoDaddy API, so it is a
provider
interface with a console sink. Recipients in ``message_deliveries`` are personal data: never log
them, and the
service must refuse to deliver to ``example.*`` addresses (seed data) unless explicitly overridden.
"""

from datetime import datetime

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

from app.db.models._common import BIGPK, business_fk, enum_check, id_column, utc_now
from app.db.session import Base

CHANNELS = ("EMAIL", "SMS", "IN_APP")
DELIVERY_STATUSES = ("QUEUED", "SENDING", "SENT", "FAILED", "SUPPRESSED")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_business_read", "business_id", "read_at"),)

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    kind: Mapped[str] = mapped_column(String(40))  # LOW_STOCK, ORDER_NEW, RECONCILIATION, ...
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class NotificationTemplate(Base):
    """``business_id`` NULL = platform default; a business row overrides it."""

    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "template_key",
            "channel",
            "locale",
            name="uq_notification_templates_scope",
        ),
        enum_check("channel", CHANNELS, "channel"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str | None] = business_fk(nullable=True)
    template_key: Mapped[str] = mapped_column(String(60))
    channel: Mapped[str] = mapped_column(String(8))
    locale: Mapped[str] = mapped_column(String(10), default="en")
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text())
    active: Mapped[bool] = mapped_column(Boolean(), default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class MessageDelivery(Base):
    __tablename__ = "message_deliveries"
    __table_args__ = (
        UniqueConstraint("business_id", "dedupe_key", name="uq_message_deliveries_business_dedupe"),
        enum_check("channel", CHANNELS, "channel"),
        enum_check("status", DELIVERY_STATUSES, "status"),
        Index("ix_message_deliveries_status_scheduled", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    business_id: Mapped[str | None] = business_fk(nullable=True)
    notification_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("notifications.id"))
    channel: Mapped[str] = mapped_column(String(8))
    provider: Mapped[str] = mapped_column(String(40))  # 'console', 'godaddy_smtp', ...
    recipient: Mapped[str] = mapped_column(String(254))
    template_key: Mapped[str | None] = mapped_column(String(60))
    subject: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), default="QUEUED")
    attempts: Mapped[int] = mapped_column(Integer(), default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    provider_message_id: Mapped[str | None] = mapped_column(String(120))
    dedupe_key: Mapped[str | None] = mapped_column(String(120))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
