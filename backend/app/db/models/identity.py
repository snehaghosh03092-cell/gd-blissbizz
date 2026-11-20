"""OWNER SERVICE: identity. Accounts, tenants, sessions, onboarding drafts.

``owners``, ``businesses``, ``browser_sessions`` and ``rate_limit_buckets`` are the B00-B02 tables
(unchanged
columns, plus additive ones). The legacy ``businesses.owner_id -> owners.id`` foreign key stays
because both
tables belong to this service; it is the only cross-table reference of its kind.
"""

from datetime import datetime

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

from app.db.models._common import BIGPK, business_fk, enum_check, id_column, new_id, utc_now
from app.db.session import Base

BUSINESS_STATUSES = ("ONBOARDING", "ACTIVE", "SUSPENDED")
ONBOARDING_STATES = (
    "DRAFT",
    "COLLECTING_DETAILS",
    "VALIDATION_PENDING",
    "NEEDS_CORRECTION",
    "READY_FOR_STORE_SETUP",
    "ACTIVE",
)
SALES_MODES = ("ONLINE", "OFFLINE", "HYBRID")
MEMBER_ROLES = ("OWNER", "MANAGER", "STAFF")


class Owner(Base):
    """A person who signs in to the admin side: owners, managers, staff, platform admins."""

    __tablename__ = "owners"
    __table_args__ = (
        UniqueConstraint("email", name="uq_owners_email"),
        UniqueConstraint("username", name="uq_owners_username"),
        CheckConstraint("platform_role IS NULL OR platform_role = 'admin'", name="platform_role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    full_name: Mapped[str] = mapped_column(String(150))
    email: Mapped[str] = mapped_column(String(254))
    username: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    platform_role: Mapped[str | None] = mapped_column(String(20))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class Business(Base):
    """The tenant anchor. Every tenant table in every service references ``businesses.id``."""

    __tablename__ = "businesses"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_businesses_slug"),
        enum_check("status", BUSINESS_STATUSES, "status"),
        enum_check("onboarding_state", ONBOARDING_STATES, "onboarding_state"),
        CheckConstraint(
            "sales_mode IS NULL OR sales_mode IN ('ONLINE', 'OFFLINE', 'HYBRID')", name="sales_mode"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("owners.id", name="fk_businesses_owner_id_owners", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(150))
    slug: Mapped[str] = mapped_column(String(63))
    description: Mapped[str | None] = mapped_column(Text())
    contact_email: Mapped[str | None] = mapped_column(String(254))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text())
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    logo_path: Mapped[str | None] = mapped_column(String(512))
    legal_name: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(80))
    constitution: Mapped[str | None] = mapped_column(String(40))
    sales_mode: Mapped[str | None] = mapped_column(String(10))
    state_code: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(6))
    onboarding_state: Mapped[str] = mapped_column(
        String(30), default="ACTIVE", server_default="ACTIVE"
    )
    status: Mapped[str] = mapped_column(String(15), default="ACTIVE", server_default="ACTIVE")
    suspended_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class BusinessMember(Base):
    __tablename__ = "business_members"
    __table_args__ = (
        UniqueConstraint("business_id", "owner_id", name="uq_business_members_business_owner"),
        enum_check("role", MEMBER_ROLES, "role"),
        Index("ix_business_members_owner_id", "owner_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("owners.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))
    invited_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("owners.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class BusinessTaxProfile(Base):
    """GST profile. ``FORMAT_VALID`` means syntax/state/checksum only, never government-verified."""

    __tablename__ = "business_tax_profiles"
    __table_args__ = (
        CheckConstraint("gst_registered = 0 OR gstin IS NOT NULL", name="gstin_required"),
        enum_check(
            "verification_status",
            ("UNVERIFIED", "FORMAT_VALID", "OFFICIALLY_VERIFIED"),
            "verification_status",
        ),
        CheckConstraint(
            "verification_status <> 'OFFICIALLY_VERIFIED' OR verification_source IS NOT NULL",
            name="verified_needs_source",
        ),
        enum_check("taxpayer_scheme", ("REGULAR", "COMPOSITION", "UNKNOWN"), "taxpayer_scheme"),
        CheckConstraint("fy_start_month BETWEEN 1 AND 12", name="fy_start_month"),
    )

    business_id: Mapped[str] = business_fk(primary_key=True)
    gst_registered: Mapped[bool] = mapped_column(Boolean(), default=False)
    gstin: Mapped[str | None] = mapped_column(String(15))
    gstin_state_code: Mapped[str | None] = mapped_column(String(2))
    verification_status: Mapped[str] = mapped_column(String(20), default="UNVERIFIED")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime())
    verification_source: Mapped[str | None] = mapped_column(String(60))
    taxpayer_scheme: Mapped[str] = mapped_column(String(15), default="UNKNOWN")
    registration_state: Mapped[str | None] = mapped_column(String(2))
    fy_start_month: Mapped[int] = mapped_column(Integer(), default=4)
    invoice_prefix: Mapped[str | None] = mapped_column(String(8))
    # Indian B2C prices are normally tax-inclusive (MRP); the tax engine reads this flag.
    prices_include_tax: Mapped[bool] = mapped_column(Boolean(), default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class Customer(Base):
    """A shopper account. One account per business (no global shopper identity)."""

    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "email", name="uq_customers_business_email"),)

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    email: Mapped[str] = mapped_column(String(254))
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(32))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime())
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime())
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class CustomerAddress(Base):
    __tablename__ = "customer_addresses"
    __table_args__ = (
        Index("ix_customer_addresses_business_customer", "business_id", "customer_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE")
    )
    label: Mapped[str | None] = mapped_column(String(40))
    recipient: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(32))
    line1: Mapped[str] = mapped_column(String(200))
    line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(80))
    state_code: Mapped[str] = mapped_column(String(2))
    postal_code: Mapped[str] = mapped_column(String(6))
    is_default: Mapped[bool] = mapped_column(Boolean(), default=False)


class BrowserSession(Base):
    """Owner, customer or anonymous session. At most one of owner_id/customer_id is set (service
    rule:
    a CHECK would conflict with the owner foreign key's ON DELETE CASCADE in MySQL)."""

    __tablename__ = "browser_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(43))
    owner_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("owners.id", name="fk_browser_sessions_owner_id_owners", ondelete="CASCADE"),
        index=True,
    )
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), index=True)


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_count: Mapped[int] = mapped_column(Integer())
    expires_at: Mapped[datetime] = mapped_column(DateTime(), index=True)


class OnboardingDraft(Base):
    __tablename__ = "onboarding_drafts"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_onboarding_drafts_token_hash"),
        enum_check("intent", ("NEW", "EXISTING", "EXPLORE"), "intent"),
        enum_check("state", ONBOARDING_STATES, "state"),
        Index("ix_onboarding_drafts_owner_id", "owner_id"),
    )

    id: Mapped[str] = id_column()
    token_hash: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("owners.id"))
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id"))
    intent: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(30), default="DRAFT")
    draft: Mapped[dict] = mapped_column(JSON, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer(), default=1)
    validation_errors: Mapped[dict | list | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class OnboardingMessage(Base):
    __tablename__ = "onboarding_messages"
    __table_args__ = (
        enum_check("role", ("USER", "ASSISTANT"), "role"),
        Index("ix_onboarding_messages_draft_id", "draft_id"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("onboarding_drafts.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text())
    patch: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
