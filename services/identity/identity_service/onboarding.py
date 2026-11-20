"""Business sign-up and guided onboarding (docs/04 section 1).

    DRAFT -> COLLECTING_DETAILS -> (submit) -> READY_FOR_STORE_SETUP -> (shop published) -> ACTIVE
                   ^                    \\-> NEEDS_CORRECTION (something required missing)
                   \\________________________________/

* Sign-up creates the owner, the business (status ONBOARDING) and the OWNER membership in one
  transaction, then turns the visitor's session into the owner's (new token: no fixation).
* The guided form is a series of *patches*. Every field is validated on its own; unknown fields
  are rejected; answered fields are never asked again. The next questions (at most two) come from
  a fixed required-field policy, not from guesswork.
* GSTIN: format, state code and checksum are checked -> FORMAT_VALID. That is never called
  "verified" (there is no government lookup), and the GSTIN state must match the business state.
* Never asked: PAN, Aadhaar, bank details.
"""

import re
from typing import Annotated, Literal

from app.core.gstin import STATE_CODES, validate_gstin
from app.core.slugs import SlugPolicy
from app.db.models import AuditEvent, Business, BusinessMember, BusinessTaxProfile, Owner
from app.schemas.registrations import normalize_email
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

STATES = {code: name for code, name in STATE_CODES.items() if code not in ("97", "99")}
CATEGORIES = (
    "clothing",
    "groceries",
    "electronics",
    "gaming",
    "food",
    "beauty",
    "home",
    "books",
    "handicrafts",
    "other",
)
CONSTITUTIONS = ("PROPRIETORSHIP", "PARTNERSHIP", "LLP", "PRIVATE_LIMITED", "OTHER")
POSTAL = re.compile(r"[1-9][0-9]{5}")
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]

# field -> the question asked when it is missing (order = asking order)
REQUIRED = (
    ("category", "What do you sell? (for example clothing or groceries)"),
    ("state_code", "Which state is the business in? (it decides CGST+SGST or IGST)"),
    ("city", "Which city is the business in?"),
    ("sales_mode", "Do you sell online, in a physical shop, or both?"),
    ("contact", "How can customers reach you: an email or a phone number?"),
)


class RegisterOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: Name
    email: str = Field(max_length=254)
    username: str | None = Field(default=None, max_length=32)
    password: SecretStr = Field(min_length=15, max_length=128)
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("username")
    @classmethod
    def _username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_]{2,31}", value):
            raise ValueError(
                "Use 3–32 letters, digits or underscores, starting with a letter or digit."
            )
        return value


class RegisterBusiness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    slug: str = Field(min_length=1, max_length=63)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return SlugPolicy.normalize(value)


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_token: str | None = Field(default=None, max_length=128)
    owner: RegisterOwner
    # Kept optional so an older client can still make an account and shop in one request.
    business: RegisterBusiness | None = None
    client_ip: str = Field(default="unknown", max_length=64)


class GstinIn(BaseModel):
    gstin: str = Field(min_length=1, max_length=20)
    state_code: str | None = Field(default=None, max_length=2)


class CreateBusinessIn(RegisterBusiness):
    pass


class OnboardingPatch(BaseModel):
    """Every field optional; whatever is sent is validated alone and merged."""

    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    legal_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = (
        None
    )
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None
    category: Literal[CATEGORIES] | None = None  # type: ignore[valid-type]
    constitution: Literal[CONSTITUTIONS] | None = None  # type: ignore[valid-type]
    sales_mode: Literal["ONLINE", "OFFLINE", "HYBRID"] | None = None
    state_code: str | None = Field(default=None, max_length=2)
    city: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] | None = None
    postal_code: str | None = Field(default=None, max_length=6)
    address: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None
    contact_email: str | None = Field(default=None, max_length=254)
    contact_phone: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)] | None
    ) = None
    gst_registered: bool | None = None
    gstin: str | None = Field(default=None, max_length=20)
    prices_include_tax: bool | None = None

    @field_validator("state_code")
    @classmethod
    def _state(cls, value):
        if value and value not in STATES:
            raise ValueError("Unknown state code.")
        return value or None

    @field_validator("postal_code")
    @classmethod
    def _postal(cls, value):
        if value and not POSTAL.fullmatch(value):
            raise ValueError("Enter a 6-digit PIN code.")
        return value or None

    @field_validator("contact_email")
    @classmethod
    def _contact_email(cls, value):
        return normalize_email(value) if value else None


def gstin_result(gstin: str, state_code: str | None) -> dict:
    r = validate_gstin(gstin)
    reasons = {
        "bad_length": "A GSTIN has 15 characters.",
        "bad_format": "This is not in GSTIN format.",
        "unknown_state": "The first two digits are not an Indian state code.",
        "bad_checksum": "The last character does not match (typo?).",
    }
    out = {
        "gstin": r.normalized or gstin.strip().upper(),
        "ok": r.ok,
        "status": "FORMAT_VALID" if r.ok else "INVALID",
        "state_code": r.state_code,
        "state": r.state_name,
        "pan": r.pan,
        "message": "Format, state code and checksum are valid (not a government verification)."
        if r.ok
        else reasons.get(r.error or "", "Not a valid GSTIN."),
    }
    if r.ok and state_code and r.state_code != state_code:
        out.update(
            ok=False,
            status="STATE_MISMATCH",
            message=f"This GSTIN is for {r.state_name}, but the business is in "
            f"{STATES.get(state_code)}.",
        )
    return out


def view(db: Session, b: Business) -> dict:
    tax = db.get(BusinessTaxProfile, b.id)
    fields = {
        "name": b.name,
        "slug": b.slug,
        "legal_name": b.legal_name,
        "description": b.description,
        "category": b.category,
        "constitution": b.constitution,
        "sales_mode": b.sales_mode,
        "state_code": b.state_code,
        "city": b.city,
        "postal_code": b.postal_code,
        "address": b.address,
        "contact_email": b.contact_email,
        "contact_phone": b.contact_phone,
        "gst_registered": bool(tax and tax.gst_registered),
        "gstin": tax.gstin if tax else None,
        "gstin_status": tax.verification_status if tax else "UNVERIFIED",
        "prices_include_tax": tax.prices_include_tax if tax else True,
    }
    missing = missing_of(fields)
    return {
        "business_id": b.id,
        "status": b.status,
        "state": b.onboarding_state,
        "fields": fields,
        "missing": [k for k, _ in missing],
        "next_questions": [q for _, q in missing[:2]],
        "complete": not missing,
        "states": STATES,
        "categories": CATEGORIES,
    }


def missing_of(fields: dict) -> list[tuple[str, str]]:
    out = []
    for key, question in REQUIRED:
        value = (
            (fields.get("contact_email") or fields.get("contact_phone"))
            if key == "contact"
            else fields.get(key)
        )
        if not value:
            out.append((key, question))
    if fields.get("gst_registered") and (
        not fields.get("gstin") or fields.get("gstin_status") != "FORMAT_VALID"
    ):
        out.append(("gstin", "What is your GSTIN? (you said the business is GST-registered)"))
    return out


def audit(
    db: Session, ctx: InternalContext, action: str, business_id: str | None, before=None, after=None
) -> None:
    db.add(
        AuditEvent(
            service="identity",
            business_id=business_id,
            actor_type=ctx.actor_type,
            actor_id=ctx.actor_id,
            action=action,
            entity_type="business",
            entity_id=business_id,
            before=before,
            after=after,
            request_id=ctx.request_id,
        )
    )


def install_onboarding_routes(
    app: FastAPI, *, Ctx, Db, sessions, register_throttle, passwords
) -> None:
    def owned(db: Session, ctx: InternalContext, lock=False) -> Business:
        if not ctx.business_id or ctx.actor_type != "OWNER":
            raise ApiError(404, "not_found", "Not found.")
        query = select(Business).where(Business.id == ctx.business_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        b = db.scalar(query)
        if b is None:
            raise ApiError(404, "not_found", "Not found.")
        return b

    @app.get("/internal/v1/slugs/{slug}")
    def slug_available(slug: str, ctx: Ctx, db: Db):
        normal = SlugPolicy.normalize(slug)
        valid = bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", normal))
        taken = db.scalar(select(func.count()).select_from(Business).where(Business.slug == normal))
        available = valid and not SlugPolicy.is_reserved(normal) and not taken
        reason = None if available else ("taken" if taken else "reserved" if valid else "invalid")
        return {"slug": normal, "available": available, "reason": reason}

    @app.post("/internal/v1/gstin/check")
    def gstin_check(body: GstinIn, ctx: Ctx):
        return gstin_result(body.gstin, body.state_code)

    def create_draft_business(
        db: Session, owner: Owner, business_input: RegisterBusiness, ctx: InternalContext
    ) -> Business:
        """Create the minimal tenant records needed before website setup begins."""
        slug = business_input.slug
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", slug) or SlugPolicy.is_reserved(
            slug
        ):
            raise ApiError(
                422,
                "validation_error",
                "Choose another web address.",
                {"business.slug": "Use 3-63 letters, digits or hyphens; some words are reserved."},
            )
        if db.scalar(select(Business.id).where(Business.slug == slug)):
            raise ApiError(
                409,
                "slug_unavailable",
                "This web address is already taken.",
                {"business.slug": "Choose another address."},
            )
        business = Business(
            owner_id=owner.id,
            name=business_input.name,
            slug=slug,
            status="ONBOARDING",
            onboarding_state="DRAFT",
            contact_email=owner.email,
        )
        db.add(business)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise ApiError(409, "slug_unavailable", "This web address was just taken.") from None
        db.add(BusinessMember(business_id=business.id, owner_id=owner.id, role="OWNER"))
        prefix = re.sub(r"[^A-Z0-9]", "", slug.upper())[:4] or "INV"
        db.add(
            BusinessTaxProfile(
                business_id=business.id,
                gst_registered=False,
                invoice_prefix=prefix,
                prices_include_tax=True,
            )
        )
        audit(
            db,
            InternalContext(ctx.request_id, "OWNER", business.id, owner.id),
            "business.registered",
            business.id,
            after={"name": business.name, "slug": slug},
        )
        return business

    @app.post("/internal/v1/auth/register", status_code=201)
    def register(body: RegisterIn, ctx: Ctx, db: Db):
        register_throttle.enforce(body.client_ip, "register-business")
        svc = sessions(db)
        current = svc.current(body.raw_token, lock=True)
        if current is None:
            raise ApiError(401, "authentication_required", "A valid session is required.")
        if current.owner_id is not None or current.customer_id is not None:
            raise ApiError(
                409, "already_authenticated", "Sign out before creating another account."
            )
        if db.scalar(select(Owner.id).where(Owner.email == body.owner.email)):
            raise ApiError(
                409,
                "email_unavailable",
                "This email is already registered.",
                {"owner.email": "Sign in instead, or use another email."},
            )
        if body.owner.username and db.scalar(
            select(Owner.id).where(Owner.username == body.owner.username)
        ):
            raise ApiError(
                409,
                "username_unavailable",
                "Choose another username.",
                {"owner.username": "This username is already taken."},
            )
        owner = Owner(
            full_name=body.owner.full_name,
            email=body.owner.email,
            username=body.owner.username,
            phone=body.owner.phone or None,
            password_hash=passwords.hash(body.owner.password.get_secret_value()),
        )
        db.add(owner)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise ApiError(
                409, "account_unavailable", "This email or username was just taken."
            ) from None
        business = create_draft_business(db, owner, body.business, ctx) if body.business else None
        try:
            credentials = svc.rotate(body.raw_token, owner.id)
            db.commit()
        except IntegrityError:  # a parallel sign-up took the email or address first
            db.rollback()
            raise ApiError(
                409, "account_unavailable", "This email, username or web address was just taken."
            ) from None
        record = credentials.record
        return {
            "raw_token": credentials.raw_token,
            "csrf_token": record.csrf_token,
            "max_age": svc.remaining_seconds(record),
            "expires_at": record.expires_at.isoformat(),
            "owner": {
                "id": owner.id,
                "full_name": owner.full_name,
                "email": owner.email,
                "platform_role": None,
            },
            "business": None
            if business is None
            else {
                "id": business.id,
                "name": business.name,
                "slug": business.slug,
                "role": "OWNER",
                "status": "ONBOARDING",
            },
        }

    @app.post("/internal/v1/businesses", status_code=201)
    def create_business(body: CreateBusinessIn, ctx: Ctx, db: Db):
        if ctx.actor_type != "OWNER" or not ctx.actor_id:
            raise ApiError(401, "authentication_required", "A valid owner session is required.")
        owner = db.get(Owner, ctx.actor_id)
        if owner is None or owner.disabled_at is not None:
            raise ApiError(401, "authentication_required", "A valid owner session is required.")
        business = create_draft_business(db, owner, body, ctx)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ApiError(409, "slug_unavailable", "This web address was just taken.") from None
        return {
            "business": {
                "id": business.id,
                "name": business.name,
                "slug": business.slug,
                "role": "OWNER",
                "status": "ONBOARDING",
            }
        }

    @app.get("/internal/v1/onboarding")
    def get_onboarding(ctx: Ctx, db: Db):
        ctx.require_permission("business.read")
        return view(db, owned(db, ctx))

    @app.patch("/internal/v1/onboarding")
    def patch_onboarding(body: OnboardingPatch, ctx: Ctx, db: Db):
        ctx.require_permission("business.write")
        b = owned(db, ctx, lock=True)
        changes = body.model_dump(exclude_unset=True)
        tax = db.get(BusinessTaxProfile, b.id) or BusinessTaxProfile(
            business_id=b.id, gst_registered=False
        )
        db.add(tax)
        state = changes.get("state_code", b.state_code)
        registered = changes.get("gst_registered", tax.gst_registered)
        gstin = changes.get("gstin", tax.gstin) if "gstin" in changes else tax.gstin
        if registered and not gstin:
            raise ApiError(
                422,
                "validation_error",
                "Enter the GSTIN to turn on GST.",
                {"gstin": "Required when the business is GST-registered."},
            )
        if gstin and ("gstin" in changes or "state_code" in changes or "gst_registered" in changes):
            result = gstin_result(gstin, state)
            if not result["ok"]:
                raise ApiError(
                    422, "validation_error", "Check the GSTIN.", {"gstin": result["message"]}
                )
            tax.gstin, tax.gstin_state_code = result["gstin"], result["state_code"]
            tax.registration_state, tax.verification_status = result["state_code"], "FORMAT_VALID"
        elif "gstin" in changes:  # cleared
            tax.gstin, tax.gstin_state_code, tax.verification_status = None, None, "UNVERIFIED"
        tax.gst_registered = bool(registered and tax.gstin)
        if tax.gst_registered:
            tax.taxpayer_scheme = "REGULAR"
        if changes.get("prices_include_tax") is not None:
            tax.prices_include_tax = changes["prices_include_tax"]
        for key in (
            "name",
            "legal_name",
            "description",
            "category",
            "constitution",
            "sales_mode",
            "state_code",
            "city",
            "postal_code",
            "address",
            "contact_email",
            "contact_phone",
        ):
            if key in changes and not (key == "name" and changes[key] is None):
                setattr(b, key, changes[key])
        db.flush()
        out = view(db, b)
        if b.onboarding_state in ("ACTIVE", "READY_FOR_STORE_SETUP") and not out["complete"]:
            db.rollback()
            raise ApiError(
                422,
                "validation_error",
                "These details are required for your shop.",
                {k: "Required." for k in out["missing"]},
            )
        if b.onboarding_state in ("DRAFT", "NEEDS_CORRECTION"):
            b.onboarding_state = "COLLECTING_DETAILS"
        out["state"] = b.onboarding_state
        db.commit()
        return out

    @app.post("/internal/v1/onboarding/submit")
    def submit(ctx: Ctx, db: Db):
        ctx.require_permission("business.write")
        b = owned(db, ctx, lock=True)
        out = view(db, b)
        if b.onboarding_state in ("READY_FOR_STORE_SETUP", "ACTIVE"):
            return out
        if not out["complete"]:
            b.onboarding_state = "NEEDS_CORRECTION"
            db.commit()
            raise ApiError(
                422,
                "onboarding_incomplete",
                "A few details are still needed.",
                dict(missing_of(out["fields"])),
            )
        b.onboarding_state = "READY_FOR_STORE_SETUP"
        audit(db, ctx, "business.details_completed", b.id)
        db.commit()
        return view(db, b)

    @app.post("/internal/v1/onboarding/activate")
    def activate(ctx: Ctx, db: Db):
        """Called once the shop is published: the business is open for trade."""
        ctx.require_permission("storefront.publish")
        b = owned(db, ctx, lock=True)
        if b.status == "SUSPENDED":
            raise ApiError(403, "business_suspended", "This business is suspended.")
        if b.onboarding_state not in ("READY_FOR_STORE_SETUP", "ACTIVE"):
            raise ApiError(409, "onboarding_incomplete", "Finish the business details first.")
        if b.status != "ACTIVE" or b.onboarding_state != "ACTIVE":
            audit(
                db,
                ctx,
                "business.activated",
                b.id,
                before={"status": b.status},
                after={"status": "ACTIVE"},
            )
            b.onboarding_state, b.status = "ACTIVE", "ACTIVE"
        db.commit()
        return view(db, b)
