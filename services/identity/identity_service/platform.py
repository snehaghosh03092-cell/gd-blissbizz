"""Platform administration (docs/08 section 6): only for the gateway's PLATFORM_ADMIN context,
which it builds only for owners with ``platform_role = 'admin'``.

Suspending a business closes its shop (the storefront answers 404) and blocks its owners'
admin access; nothing is deleted. Every suspend/reinstate is written to the append-only audit log
with who, when, why, before and after.
"""

from typing import Annotated

from app.db.models import AuditEvent, Business, BusinessMember, Customer, Owner, utc_now
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from identity_service.onboarding import audit


class SuspendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=255)


def install_platform_routes(app: FastAPI, *, Ctx, Db) -> None:
    def admin(ctx: InternalContext) -> None:
        if ctx.actor_type != "PLATFORM_ADMIN" or not ctx.actor_id:
            raise ApiError(403, "forbidden", "Platform administrators only.")

    def row(b: Business, owner_email: str | None, members: int, customers: int) -> dict:
        return {
            "id": b.id,
            "name": b.name,
            "slug": b.slug,
            "status": b.status,
            "onboarding_state": b.onboarding_state,
            "category": b.category,
            "city": b.city,
            "owner_email": owner_email,
            "members": members,
            "customers": customers,
            "suspended_reason": b.suspended_reason,
            "created_at": b.created_at.isoformat(),
        }

    @app.get("/internal/v1/platform/summary")
    def summary(ctx: Ctx, db: Db):
        admin(ctx)
        by_status = dict(
            db.execute(select(Business.status, func.count()).group_by(Business.status)).all()
        )
        return {
            "businesses": sum(by_status.values()),
            "by_status": by_status,
            "owners": db.scalar(select(func.count()).select_from(Owner)),
            "customers": db.scalar(select(func.count()).select_from(Customer)),
        }

    @app.get("/internal/v1/platform/businesses")
    def businesses(
        ctx: Ctx,
        db: Db,
        q: Annotated[str | None, Query(max_length=100)] = None,
        status: Annotated[str | None, Query(max_length=15)] = None,
    ):
        admin(ctx)
        members = (
            select(BusinessMember.business_id, func.count().label("n"))
            .group_by(BusinessMember.business_id)
            .subquery()
        )
        customers = (
            select(Customer.business_id, func.count().label("n"))
            .group_by(Customer.business_id)
            .subquery()
        )
        query = (
            select(Business, Owner.email, members.c.n, customers.c.n)
            .outerjoin(Owner, Owner.id == Business.owner_id)
            .outerjoin(members, members.c.business_id == Business.id)
            .outerjoin(customers, customers.c.business_id == Business.id)
        )
        if status:
            query = query.where(Business.status == status.upper())
        if q:
            pattern = f"%{q.strip().lower()}%"
            query = query.where(
                func.lower(Business.name).like(pattern) | Business.slug.like(pattern)
            )
        rows = db.execute(query.order_by(Business.created_at.desc()).limit(500)).all()
        return {"items": [row(b, email, m or 0, c or 0) for b, email, m, c in rows]}

    def change(db, ctx: InternalContext, business_id: str, to: str, reason: str | None) -> dict:
        b = db.scalar(select(Business).where(Business.id == business_id).with_for_update())
        if b is None:
            raise ApiError(404, "not_found", "Not found.")
        if b.status == to:
            return row(b, None, 0, 0)
        before = {"status": b.status, "reason": b.suspended_reason}
        if to == "SUSPENDED":
            b.status, b.suspended_reason = "SUSPENDED", reason
        else:  # back to where it was in its life: open for trade, or still onboarding
            if b.status != "SUSPENDED":
                raise ApiError(
                    409, "invalid_transition", "Only suspended businesses can be reinstated."
                )
            b.status = "ACTIVE" if b.onboarding_state == "ACTIVE" else "ONBOARDING"
            b.suspended_reason = None
        b.updated_at = utc_now()
        audit(
            db,
            ctx,
            "business.suspended" if to == "SUSPENDED" else "business.reinstated",
            b.id,
            before=before,
            after={"status": b.status, "reason": reason},
        )
        db.commit()
        return row(b, None, 0, 0)

    @app.post("/internal/v1/platform/businesses/{business_id}/suspend")
    def suspend(business_id: str, body: SuspendIn, ctx: Ctx, db: Db):
        admin(ctx)
        return change(db, ctx, business_id, "SUSPENDED", body.reason)

    @app.post("/internal/v1/platform/businesses/{business_id}/reinstate")
    def reinstate(business_id: str, ctx: Ctx, db: Db):
        admin(ctx)
        return change(db, ctx, business_id, "ACTIVE", None)

    @app.get("/internal/v1/platform/audit")
    def audit_log(
        ctx: Ctx,
        db: Db,
        business_id: Annotated[str | None, Query(max_length=36)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        admin(ctx)
        query = select(AuditEvent)
        if business_id:
            query = query.where(AuditEvent.business_id == business_id)
        rows = db.scalars(query.order_by(AuditEvent.id.desc()).limit(limit)).all()
        return {
            "items": [
                {
                    "id": e.id,
                    "service": e.service,
                    "business_id": e.business_id,
                    "actor_type": e.actor_type,
                    "actor_id": e.actor_id,
                    "action": e.action,
                    "before": e.before,
                    "after": e.after,
                    "at": e.created_at.isoformat(),
                }
                for e in rows
            ]
        }
