"""Identity service.

Every endpoint except /health requires a signed internal request (docs/02 section 4.1).
Reuses the B00-B02 building blocks (Argon2 passwords, hashed session tokens with rotation,
SQL rate limits). Raw session tokens are returned only at issue time; the gateway turns them
into cookies.
"""

import hashlib
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

from app.core.config import settings as platform_settings
from app.core.errors import ApiError as LegacyApiError
from app.core.passwords import PasswordService
from app.db.models import Business, BusinessMember, Customer, Owner
from app.db.rate_limits import RateLimitRepository
from app.db.session import RateLimitSessionLocal, get_db
from app.db.sessions import SessionRepository
from app.services.sessions import SessionService
from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError, error_body, register_exception_handlers
from blissbiz_common.fastapi import make_internal_dependency
from blissbiz_common.logging import configure_logging, install_request_id
from fastapi import Depends, FastAPI, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from identity_service.customers import customer_out, install_customer_routes
from identity_service.onboarding import install_onboarding_routes
from identity_service.platform import install_platform_routes


class IdentitySettings(ServiceSettings):
    service_name: str = "identity"
    login_rate_limit: int = Field(default=10, ge=1)
    login_rate_window_seconds: int = Field(default=900, ge=1)
    register_rate_limit: int = Field(default=20, ge=1)
    customer_session_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=60, le=30 * 24 * 3600)


# ---- schemas ---------------------------------------------------------------
class SessionIn(BaseModel):
    raw_token: str | None = Field(default=None, max_length=128)


class SessionOut(BaseModel):
    raw_token: str
    csrf_token: str
    max_age: int
    expires_at: datetime


class LoginIn(BaseModel):
    raw_token: str = Field(max_length=128)
    email: str = Field(max_length=254)
    password: SecretStr = Field(max_length=128)
    client_ip: str = Field(default="unknown", max_length=64)


class StorefrontOwnerLoginIn(LoginIn):
    business_id: str = Field(min_length=1, max_length=36)


class TokenIn(BaseModel):
    raw_token: str | None = Field(default=None, max_length=128)


class OwnerOut(BaseModel):
    id: str
    full_name: str
    email: str
    platform_role: str | None = None


class BusinessRef(BaseModel):
    id: str
    name: str
    slug: str
    role: str
    status: str


class ResolveOut(BaseModel):
    kind: Literal["none", "anonymous", "owner", "customer"]
    csrf_token: str | None = None
    owner: OwnerOut | None = None
    businesses: list[BusinessRef] = []
    customer: dict | None = None  # customer sessions: the account, valid only for business_id
    business_id: str | None = None


class LoginOut(SessionOut):
    owner: OwnerOut
    businesses: list[BusinessRef]


# ---- helpers ---------------------------------------------------------------
def memberships(db: Session, owner_id: str) -> list[BusinessRef]:
    """Businesses an owner may act in: explicit memberships plus businesses they registered."""
    refs: dict[str, BusinessRef] = {}
    rows = db.execute(
        select(Business, BusinessMember.role)
        .join(BusinessMember, BusinessMember.business_id == Business.id)
        .where(BusinessMember.owner_id == owner_id)
    ).all()
    for business, role in rows:
        refs[business.id] = _ref(business, role)
    for business in db.scalars(select(Business).where(Business.owner_id == owner_id)):
        refs.setdefault(business.id, _ref(business, "OWNER"))  # registered before members existed
    return sorted(refs.values(), key=lambda r: r.name.lower())


def _ref(business: Business, role: str) -> BusinessRef:
    return BusinessRef(
        id=business.id, name=business.name, slug=business.slug, role=role, status=business.status
    )


def _owner_out(owner: Owner) -> OwnerOut:
    return OwnerOut(
        id=owner.id, full_name=owner.full_name, email=owner.email, platform_role=owner.platform_role
    )


class LoginThrottle:
    """Counts login attempts per (client, email) on its own pool so failures still count."""

    def __init__(self, factory: Callable[[], Session], limit: int, window: int):
        self.factory, self.limit, self.window = factory, limit, window

    def enforce(self, client_ip: str, email: str) -> None:
        start = int(time.time()) // self.window * self.window
        key = hashlib.sha256(f"login:{client_ip}:{email}:{start}".encode()).hexdigest()
        end = start + self.window
        with self.factory() as session, session.begin():
            allowed = RateLimitRepository(session).consume(
                key, datetime.fromtimestamp(end, UTC).replace(tzinfo=None), self.limit
            )
        if not allowed:
            retry = max(1, math.ceil(end - time.time()))
            raise ApiError(
                429,
                "rate_limited",
                "Too many login attempts. Try again later.",
                headers={"Retry-After": str(retry)},
            )


# ---- app -------------------------------------------------------------------
def create_app(
    settings: IdentitySettings | None = None,
    rate_limit_factory: Callable[[], Session] = RateLimitSessionLocal,
    configure_logs: bool = False,
) -> FastAPI:
    settings = settings or IdentitySettings()
    if configure_logs:
        configure_logging(settings.service_name, settings.log_level)
    internal = make_internal_dependency(settings)
    throttle = LoginThrottle(
        rate_limit_factory, settings.login_rate_limit, settings.login_rate_window_seconds
    )
    passwords = PasswordService()
    dummy_hash = passwords.hash(
        "not-a-real-password-for-timing"
    )  # equalise timing for unknown emails

    app = FastAPI(
        title="BlissBiz identity", docs_url=None, redoc_url=None, openapi_url="/openapi.json"
    )
    install_request_id(app)
    register_exception_handlers(app)

    @app.exception_handler(LegacyApiError)  # errors raised by the reused B01/B02 services
    async def _legacy(_, exc: LegacyApiError):
        return JSONResponse(
            error_body(exc.code, exc.message, exc.fields),
            status_code=exc.status_code,
            headers={**exc.headers, "Cache-Control": "no-store"},
        )

    def sessions(db: Session) -> SessionService:
        return SessionService(SessionRepository(db), platform_settings)

    Ctx = Annotated[InternalContext, Depends(internal)]
    Db = Annotated[Session, Depends(get_db)]

    @app.get("/health")
    def health():  # liveness only; used by the dev runner
        return {"status": "ok", "service": settings.service_name}

    @app.get("/internal/v1/health")
    def ready(ctx: Ctx, db: Db):  # readiness: signing works and the database answers
        db.execute(text("SELECT 1"))
        return {"status": "ready", "service": settings.service_name}

    @app.post("/internal/v1/auth/session", response_model=SessionOut)
    def start_session(body: SessionIn, ctx: Ctx, db: Db):
        svc = sessions(db)
        credentials = svc.bootstrap(body.raw_token)
        record = credentials.record
        out = SessionOut(
            raw_token=credentials.raw_token,
            csrf_token=record.csrf_token,
            max_age=svc.remaining_seconds(record),
            expires_at=record.expires_at,
        )
        db.commit()
        return out

    @app.post("/internal/v1/auth/login", response_model=LoginOut)
    def login(body: LoginIn, ctx: Ctx, db: Db):
        identifier = body.email.strip().lower()
        throttle.enforce(body.client_ip, identifier)
        svc = sessions(db)
        current = svc.current(body.raw_token, lock=True)
        if current is None:
            raise ApiError(401, "authentication_required", "A valid session is required.")
        if current.owner_id is not None or current.customer_id is not None:
            raise ApiError(409, "already_authenticated", "Sign out before signing in again.")
        field = Owner.email if "@" in identifier else Owner.username
        owner = db.scalar(select(Owner).where(field == identifier))
        password = body.password.get_secret_value()
        valid = passwords.verify(owner.password_hash if owner else dummy_hash, password)
        if owner is None or owner.disabled_at is not None or not valid:
            raise ApiError(401, "invalid_credentials", "Invalid email, username or password.")
        credentials = svc.rotate(body.raw_token, owner.id)  # new token: no session fixation
        record = credentials.record
        out = LoginOut(
            raw_token=credentials.raw_token,
            csrf_token=record.csrf_token,
            max_age=svc.remaining_seconds(record),
            expires_at=record.expires_at,
            owner=_owner_out(owner),
            businesses=memberships(db, owner.id),
        )
        db.commit()
        return out

    @app.post("/internal/v1/auth/storefront-owner-login", response_model=LoginOut)
    def storefront_owner_login(body: StorefrontOwnerLoginIn, ctx: Ctx, db: Db):
        """Sign an owner in from a published shop, but only for one of their own businesses.

        The membership check happens before rotating the anonymous session so an owner cannot
        use another tenant's storefront as a general owner-login endpoint.
        """
        identifier = body.email.strip().lower()
        throttle.enforce(body.client_ip, identifier)
        svc = sessions(db)
        current = svc.current(body.raw_token, lock=True)
        if current is None:
            raise ApiError(401, "authentication_required", "A valid session is required.")
        if current.owner_id is not None or current.customer_id is not None:
            raise ApiError(409, "already_authenticated", "Sign out before signing in again.")
        field = Owner.email if "@" in identifier else Owner.username
        owner = db.scalar(select(Owner).where(field == identifier))
        password = body.password.get_secret_value()
        valid = passwords.verify(owner.password_hash if owner else dummy_hash, password)
        businesses = memberships(db, owner.id) if owner is not None else []
        membership = next((b for b in businesses if b.id == body.business_id), None)
        if (
            owner is None
            or owner.disabled_at is not None
            or not valid
            or membership is None
            or membership.status != "ACTIVE"
        ):
            raise ApiError(401, "invalid_credentials", "Invalid email or password.")
        credentials = svc.rotate(body.raw_token, owner.id)
        record = credentials.record
        out = LoginOut(
            raw_token=credentials.raw_token,
            csrf_token=record.csrf_token,
            max_age=svc.remaining_seconds(record),
            expires_at=record.expires_at,
            owner=_owner_out(owner),
            businesses=businesses,
        )
        db.commit()
        return out

    @app.post("/internal/v1/auth/logout", status_code=204)
    def logout(body: TokenIn, ctx: Ctx, db: Db):
        sessions(db).revoke(body.raw_token)
        db.commit()
        return Response(status_code=204)

    @app.post("/internal/v1/sessions/resolve", response_model=ResolveOut)
    def resolve(body: TokenIn, ctx: Ctx, db: Db):
        record = sessions(db).current(body.raw_token)
        if record is None:
            return ResolveOut(kind="none")
        if record.customer_id is not None:
            customer = db.get(Customer, record.customer_id)
            if customer is None or customer.disabled_at is not None:
                return ResolveOut(kind="none")
            return ResolveOut(
                kind="customer",
                csrf_token=record.csrf_token,
                customer=customer_out(customer),
                business_id=record.business_id,
            )
        if record.owner_id is None:
            return ResolveOut(kind="anonymous", csrf_token=record.csrf_token)
        owner = db.get(Owner, record.owner_id)
        if owner is None or owner.disabled_at is not None:
            return ResolveOut(kind="none")
        return ResolveOut(
            kind="owner",
            csrf_token=record.csrf_token,
            owner=_owner_out(owner),
            businesses=memberships(db, owner.id),
        )

    install_customer_routes(
        app, Ctx=Ctx, Db=Db, sessions=sessions, throttle=throttle,
        register_throttle=LoginThrottle(rate_limit_factory, settings.register_rate_limit,
                                        settings.login_rate_window_seconds),
        passwords=passwords, dummy_hash=dummy_hash,
        customer_ttl=settings.customer_session_ttl_seconds,
    )  # fmt: skip
    install_onboarding_routes(
        app, Ctx=Ctx, Db=Db, sessions=sessions, passwords=passwords,
        register_throttle=LoginThrottle(rate_limit_factory, settings.register_rate_limit,
                                        settings.login_rate_window_seconds),
    )  # fmt: skip
    install_platform_routes(app, Ctx=Ctx, Db=Db)
    return app


app = create_app(configure_logs=True)
