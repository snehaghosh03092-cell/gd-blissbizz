"""Shopper accounts (docs/04 section 3): one account per business, customer sessions, addresses,
and the public business profile other services need (name, state, GST flags).

The business always comes from the signed context (the gateway resolved it from the Host header);
a customer of shop A can never sign in to shop B with the same session.
"""

import re
import secrets
from datetime import timedelta
from typing import Annotated

from app.core.gstin import STATE_CODES
from app.db.models import (
    BrowserSession,
    Business,
    BusinessTaxProfile,
    Customer,
    CustomerAddress,
    utc_now,
)
from app.schemas.registrations import normalize_email
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import FastAPI, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

MAX_ADDRESSES = 10
# GSTIN codes 97 (Other Territory) and 99 (Centre Jurisdiction) are not places you can deliver to
DELIVERY_STATES = {code: name for code, name in STATE_CODES.items() if code not in ("97", "99")}
POSTAL = re.compile(r"[1-9][0-9]{5}")
Text150 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
Text200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class CustomerRegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_token: str | None = Field(default=None, max_length=128)
    email: str = Field(max_length=254)
    password: SecretStr = Field(min_length=15, max_length=128)
    full_name: Text150
    phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)] | None = None
    client_ip: str = Field(default="unknown", max_length=64)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)


class CustomerLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_token: str | None = Field(default=None, max_length=128)
    email: str = Field(max_length=254)
    password: SecretStr = Field(max_length=128)
    client_ip: str = Field(default="unknown", max_length=64)


class AddressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    recipient: Text150
    phone: Annotated[str, StringConstraints(strip_whitespace=True, min_length=6, max_length=32)]
    line1: Text200
    line2: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    city: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    state_code: str = Field(min_length=2, max_length=2)
    postal_code: str = Field(min_length=6, max_length=6)
    is_default: bool = False

    @field_validator("state_code")
    @classmethod
    def _state(cls, value: str) -> str:
        if value not in DELIVERY_STATES:
            raise ValueError("Unknown state code.")
        return value

    @field_validator("postal_code")
    @classmethod
    def _postal(cls, value: str) -> str:
        if not POSTAL.fullmatch(value):
            raise ValueError("Enter a 6-digit PIN code.")
        return value


def customer_out(c: Customer) -> dict:
    return {"id": c.id, "full_name": c.full_name, "email": c.email, "phone": c.phone}


def address_out(a: CustomerAddress) -> dict:
    return {
        "id": a.id,
        "label": a.label,
        "recipient": a.recipient,
        "phone": a.phone,
        "line1": a.line1,
        "line2": a.line2,
        "city": a.city,
        "state_code": a.state_code,
        "state": STATE_CODES.get(a.state_code),
        "postal_code": a.postal_code,
        "is_default": a.is_default,
    }


def business_profile(db: Session, business_id: str) -> dict | None:
    business = db.get(Business, business_id)
    if business is None:
        return None
    tax = db.get(BusinessTaxProfile, business_id)
    return {
        "id": business.id,
        "name": business.name,
        "slug": business.slug,
        "status": business.status,
        "state_code": business.state_code,
        "city": business.city,
        "currency": business.currency,
        "contact_email": business.contact_email,
        "contact_phone": business.contact_phone,
        "description": business.description,
        "legal_name": business.legal_name or business.name,
        "address": business.address,
        "fy_start_month": tax.fy_start_month if tax else 4,
        "gst_registered": bool(tax and tax.gst_registered),
        "gstin": tax.gstin if tax and tax.gst_registered else None,
        "prices_include_tax": tax.prices_include_tax if tax else True,
        "invoice_prefix": (tax.invoice_prefix if tax and tax.invoice_prefix else None)
        or business.slug[:3].upper(),
    }


def install_customer_routes(
    app: FastAPI,
    *,
    Ctx,
    Db,
    sessions,
    throttle,
    register_throttle,
    passwords,
    dummy_hash,
    customer_ttl: int,
) -> None:
    def business_of(ctx: InternalContext) -> str:
        if ctx.business_id is None:
            raise ApiError(404, "not_found", "Not found.")
        return ctx.business_id

    def issue(db: Session, raw_token: str | None, customer: Customer) -> dict:
        """Replace the anonymous session with a customer session (new token: no fixation)."""
        svc = sessions(db)
        current = svc.current(raw_token, lock=True)
        if current is None:
            raise ApiError(401, "authentication_required", "A valid session is required.")
        if current.owner_id is not None or current.customer_id is not None:
            raise ApiError(409, "already_authenticated", "Sign out before signing in again.")
        svc.repository.remove(current.token_hash)
        raw, now = secrets.token_urlsafe(32), utc_now()
        record = BrowserSession(
            token_hash=svc.token_hash(raw),
            csrf_token=secrets.token_urlsafe(32),
            customer_id=customer.id,
            business_id=customer.business_id,
            created_at=now,
            expires_at=now + timedelta(seconds=customer_ttl),
        )
        svc.repository.add(record)
        return {
            "raw_token": raw,
            "csrf_token": record.csrf_token,
            "max_age": svc.remaining_seconds(record),
            "expires_at": record.expires_at.isoformat(),
            "customer": customer_out(customer),
            "business_id": customer.business_id,
        }

    def require_customer(ctx: InternalContext, db: Session) -> Customer:
        b = business_of(ctx)
        if ctx.actor_type != "CUSTOMER" or ctx.actor_id is None:
            raise ApiError(401, "authentication_required", "Please sign in.")
        customer = db.scalar(
            select(Customer).where(Customer.id == ctx.actor_id, Customer.business_id == b)
        )
        if customer is None or customer.disabled_at is not None:
            raise ApiError(401, "authentication_required", "Please sign in.")
        return customer

    @app.get("/internal/v1/businesses/{business_id}/profile")
    def profile(business_id: str, ctx: Ctx, db: Db):
        """Public facts about a business. Services may ask about any; others only their own."""
        if ctx.actor_type != "SERVICE":
            ctx.require_business(business_id)
        out = business_profile(db, business_id)
        if out is None:
            raise ApiError(404, "not_found", "Not found.")
        return out

    @app.post("/internal/v1/customers/register")
    def register(body: CustomerRegisterIn, ctx: Ctx, db: Db):
        b = business_of(ctx)
        register_throttle.enforce(body.client_ip, f"register:{b}")
        business = db.get(Business, b)
        if business is None or business.status != "ACTIVE":
            raise ApiError(404, "not_found", "Not found.")
        exists = db.scalar(
            select(Customer.id).where(Customer.business_id == b, Customer.email == body.email)
        )
        if exists:
            raise ApiError(
                409,
                "email_unavailable",
                "An account with this email already exists. Sign in instead.",
                {"email": "Already registered."},
            )
        customer = Customer(
            business_id=b,
            email=body.email,
            full_name=body.full_name,
            phone=body.phone or None,
            password_hash=passwords.hash(body.password.get_secret_value()),
        )
        db.add(customer)
        try:
            db.flush()
        except IntegrityError:  # two sign-ups with the same email at the same moment
            db.rollback()
            raise ApiError(
                409,
                "email_unavailable",
                "An account with this email already exists. Sign in instead.",
                {"email": "Already registered."},
            ) from None
        out = issue(db, body.raw_token, customer)
        db.commit()
        return out

    @app.post("/internal/v1/customers/login")
    def login(body: CustomerLoginIn, ctx: Ctx, db: Db):
        b = business_of(ctx)
        email = body.email.strip().lower()
        throttle.enforce(body.client_ip, f"{b}:{email}")
        customer = db.scalar(
            select(Customer).where(Customer.business_id == b, Customer.email == email)
        )
        password = body.password.get_secret_value()
        valid = passwords.verify(customer.password_hash if customer else dummy_hash, password)
        if customer is None or customer.disabled_at is not None or not valid:
            raise ApiError(401, "invalid_credentials", "Invalid email or password.")
        out = issue(db, body.raw_token, customer)
        db.commit()
        return out

    # ---- addresses (customer only, own rows only) ------------------------------------
    @app.get("/internal/v1/customers/me")
    def me(ctx: Ctx, db: Db):
        return customer_out(require_customer(ctx, db))

    def own_addresses(db: Session, customer: Customer):
        return select(CustomerAddress).where(
            CustomerAddress.business_id == customer.business_id,
            CustomerAddress.customer_id == customer.id,
        )

    @app.get("/internal/v1/customers/me/addresses")
    def addresses(ctx: Ctx, db: Db):
        customer = require_customer(ctx, db)
        rows = db.scalars(
            own_addresses(db, customer).order_by(
                CustomerAddress.is_default.desc(), CustomerAddress.id
            )
        ).all()
        return {"items": [address_out(a) for a in rows]}

    @app.post("/internal/v1/customers/me/addresses", status_code=201)
    def add_address(body: AddressIn, ctx: Ctx, db: Db):
        customer = require_customer(ctx, db)
        count = db.scalar(select(func.count()).select_from(own_addresses(db, customer).subquery()))
        if count >= MAX_ADDRESSES:
            raise ApiError(
                409, "too_many_addresses", f"You can save up to {MAX_ADDRESSES} addresses."
            )
        make_default = body.is_default or count == 0
        if make_default:
            db.execute(
                update(CustomerAddress)
                .where(
                    CustomerAddress.business_id == customer.business_id,
                    CustomerAddress.customer_id == customer.id,
                )
                .values(is_default=False)
            )
        address = CustomerAddress(
            business_id=customer.business_id,
            customer_id=customer.id,
            **body.model_dump(exclude={"is_default"}),
            is_default=make_default,
        )
        db.add(address)
        db.commit()
        return address_out(address)

    def find_address(db: Session, customer: Customer, address_id: str) -> CustomerAddress:
        address = db.scalar(own_addresses(db, customer).where(CustomerAddress.id == address_id))
        if address is None:
            raise ApiError(404, "not_found", "Not found.")
        return address

    @app.get("/internal/v1/customers/me/addresses/{address_id}")
    def get_address(address_id: str, ctx: Ctx, db: Db):
        return address_out(find_address(db, require_customer(ctx, db), address_id))

    @app.delete("/internal/v1/customers/me/addresses/{address_id}", status_code=204)
    def delete_address(address_id: str, ctx: Ctx, db: Db):
        customer = require_customer(ctx, db)
        address = find_address(db, customer, address_id)
        was_default = address.is_default
        db.delete(address)  # orders keep their own snapshot of the address
        db.flush()
        if was_default:
            nxt = db.scalar(own_addresses(db, customer).order_by(CustomerAddress.id).limit(1))
            if nxt is not None:
                nxt.is_default = True
        db.commit()
        return Response(status_code=204)
