"""Gateway routes for steps 12-14: business sign-up + guided setup + going live, the owner
assistant and insights, and platform administration.

Platform routes accept only a signed-in owner whose account has ``platform_role = 'admin'``; the
gateway then signs the call as PLATFORM_ADMIN. Nobody else can obtain that context.
"""

import re
import time
from collections import deque
from datetime import date
from typing import Literal

from blissbiz_common.clients import raise_for
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from blissbiz_common.registration_pack import registration_workbook
from blissbiz_common.workbooks import XLSX
from fastapi import FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, SecretStr

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(max_length=150)
    email: str = Field(max_length=254)
    username: str | None = Field(default=None, max_length=32)
    password: SecretStr = Field(max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    # Older API clients may still send these while the current signup form creates only an account.
    business_name: str | None = Field(default=None, max_length=150)
    slug: str | None = Field(default=None, max_length=63)


class CreateBusinessBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=150)
    slug: str = Field(min_length=1, max_length=63)


class GstinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gstin: str = Field(max_length=20)
    state_code: str | None = Field(default=None, max_length=2)


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    conversation_id: str | None = Field(default=None, max_length=36)


class SuspendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=255)


class RateLimiter:
    """Sliding-window limit per key, in memory (one gateway process in this local setup)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window, self.hits = limit, window, {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self.hits.setdefault(key, deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            retry = int(q[0] + self.window - now) + 1
            raise ApiError(
                429,
                "rate_limited",
                "Too many requests. Please wait a moment.",
                headers={"Retry-After": str(retry)},
            )
        q.append(now)
        if len(self.hits) > 10_000:  # forget idle keys
            for k in [k for k, v in self.hits.items() if not v][:5000]:
                self.hits.pop(k, None)


def install_extra_routes(app: FastAPI, d) -> None:
    agent_limit = RateLimiter(d.settings.agent_rate_limit, 60)
    d.agent_limit = agent_limit

    # ---- step 12: sign-up and guided setup ----------------------------------------------------
    @app.get("/api/v1/auth/slug")
    async def slug(
        request: Request, response: Response, value: str = Query(min_length=1, max_length=63)
    ):
        d.check_origin(request, mutation=False)
        d.no_store(response)
        safe = re.sub(r"[^a-zA-Z0-9-]", "-", value)[:63] or "x"
        return await d.call(d.identity, "GET", f"/internal/v1/slugs/{safe}", d.gateway_ctx())

    @app.post("/api/v1/auth/register", status_code=201)
    async def register(body: RegisterBody, request: Request, response: Response):
        """Create an owner account and sign them in.

        ``business_name`` and ``slug`` remain accepted for old local clients, but the current UI
        defers shop creation until the owner selects Create a website.
        """
        session = await d.require_csrf(request)
        if session["kind"] in ("owner", "customer"):
            raise ApiError(
                409, "already_authenticated", "Sign out before creating another account."
            )
        data = await d.call(
            d.identity,
            "POST",
            "/internal/v1/auth/register",
            d.gateway_ctx(),
            json_body={
                "raw_token": d.token_of(request),
                "owner": {
                    "full_name": body.full_name,
                    "email": body.email,
                    "username": body.username,
                    "password": body.password.get_secret_value(),
                    "phone": body.phone or None,
                },
                **(
                    {"business": {"name": body.business_name, "slug": body.slug}}
                    if body.business_name and body.slug
                    else {}
                ),
                "client_ip": request.client.host if request.client else "unknown",
            },
        )
        d.set_cookie(response, data)
        d.no_store(response)
        return {
            "csrf_token": data["csrf_token"],
            "owner": data["owner"],
            "business": data.get("business"),
            "businesses": [data["business"]] if data.get("business") else [],
        }

    @app.post("/api/v1/admin/businesses", status_code=201)
    async def create_business(body: CreateBusinessBody, request: Request, response: Response):
        """Create an owner's first (or additional) draft shop after account signup."""
        session = await d.require_csrf(request)
        if session["kind"] != "owner":
            raise ApiError(401, "authentication_required", "A valid owner session is required.")
        ctx = InternalContext(
            request_id=d.request_id(),
            actor_type="OWNER",
            business_id=None,
            actor_id=session["owner"]["id"],
            role="OWNER",
            permissions=d.permissions_for("OWNER"),
        )
        data = await d.call(
            d.identity, "POST", "/internal/v1/businesses", ctx, json_body=body.model_dump()
        )
        d.no_store(response)
        return data["business"]

    @app.post("/api/v1/business-guide")
    async def account_business_guide(body: AskBody, request: Request, response: Response):
        """Business-start guide for a signed-in owner before they have a shop."""
        session = await d.require_csrf(request)
        if session["kind"] != "owner":
            raise ApiError(401, "authentication_required", "A valid owner session is required.")
        ctx = InternalContext(
            request_id=d.request_id(),
            actor_type="OWNER",
            business_id=None,
            actor_id=session["owner"]["id"],
            role="OWNER",
            permissions=d.permissions_for("OWNER"),
        )
        d.no_store(response)
        return await d.call(
            d.agent,
            "POST",
            "/internal/v1/agent/ask",
            ctx,
            json_body={**body.model_dump(), "audience": "onboarding"},
        )

    @app.post("/api/v1/tools/gstin-check")
    async def gstin_check(body: GstinBody, request: Request, response: Response):
        """Format + state + checksum check (never a government verification)."""
        d.check_origin(request, mutation=True)
        d.no_store(response)
        return await d.call(
            d.identity,
            "POST",
            "/internal/v1/gstin/check",
            d.gateway_ctx(),
            json_body=body.model_dump(),
        )

    @app.post("/api/v1/admin/businesses/{business_id}/storefront/publish")
    async def publish(business_id: str, request: Request, response: Response):
        """Go live: the business details must be complete, then the shop is published (its address
        becomes active) and the business is marked open for trade."""
        await d.require_csrf(request)
        ctx, membership = await d.owner_context(request, business_id)
        ctx.require_permission("storefront.publish")
        onboarding = await d.call(d.identity, "GET", "/internal/v1/onboarding", ctx)
        if onboarding["state"] not in ("READY_FOR_STORE_SETUP", "ACTIVE"):
            raise ApiError(
                409,
                "onboarding_incomplete",
                "Finish your business details first.",
                {k: "Required." for k in onboarding.get("missing", [])} or None,
            )
        stock = await d.call(d.supply, "GET", "/internal/v1/warehouses", ctx)
        if not stock["items"]:
            raise ApiError(409, "warehouse_missing", "Add the place where you keep stock first.")
        result = await d.call(
            d.store,
            "POST",
            "/internal/v1/storefront/publish",
            ctx,
            json_body={"slug": membership["slug"]},
        )
        await d.call(d.identity, "POST", "/internal/v1/onboarding/activate", ctx)
        d.forget_tenant(result.get("url_host"))
        d.no_store(response)
        return {**result, "shop_url_host": result.get("url_host")}

    # ---- step 13: owner assistant ---------------------------------------------------------------
    @app.get("/api/v1/admin/businesses/{business_id}/documents/{kind}")
    async def document_download(
        business_id: str,
        kind: Literal["invoices.xlsx", "invoices.zip", "inventory.xlsx", "registration.xlsx"],
        request: Request,
        start: date | None = Query(default=None, ge=date(1900, 1, 1), le=date(9998, 12, 31)),
        end: date | None = Query(default=None, ge=date(1900, 1, 1), le=date(9998, 12, 31)),
        state_code: str | None = Query(default=None, pattern=r"^[0-9]{2}$"),
        constitution: Literal["PROPRIETORSHIP", "PARTNERSHIP", "LLP", "PRIVATE_LIMITED"]
        | None = None,
        premises: Literal["OWNED", "RENTED", "CONSENT"] | None = None,
    ):
        d.check_origin(request, mutation=False)
        ctx, _ = await d.owner_context(request, business_id)
        permission = (
            "invoices.read"
            if kind.startswith("invoices")
            else "inventory.read"
            if kind == "inventory.xlsx"
            else "business.read"
        )
        ctx.require_permission(permission)
        if kind != "registration.xlsx" and (state_code or constitution or premises):
            raise ApiError(
                422, "invalid_options", "Registration details apply only to registration workbooks."
            )
        if kind not in ("invoices.xlsx", "invoices.zip") and (start or end):
            raise ApiError(422, "invalid_period", "Date ranges apply only to invoice exports.")
        if kind == "registration.xlsx":
            profile = await d.call(d.identity, "GET", "/internal/v1/onboarding", ctx)
            if state_code and state_code not in profile.get("states", {}):
                raise ApiError(422, "invalid_state", "Choose a recognised state/UT.")
            options = {
                k: v
                for k, v in (
                    ("state_code", state_code),
                    ("constitution", constitution),
                    ("premises", premises),
                )
                if v
            }
            content = await run_in_threadpool(registration_workbook, profile, options)
        else:
            client = d.agent if kind == "inventory.xlsx" else d.billing
            params = {k: v.isoformat() for k, v in (("start", start), ("end", end)) if v}
            upstream = await client.request(
                "GET", f"/internal/v1/document-exports/{kind}", ctx, params=params
            )
            if upstream.status_code >= 400:
                raise_for(upstream)
            content = upstream.content
        return Response(
            content,
            media_type="application/zip" if kind.endswith(".zip") else XLSX,
            headers={
                "Content-Disposition": f'attachment; filename="{kind}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def owner_agent(
        business_id: str, request: Request, path: str, body: dict, *, guide_only: bool = False
    ) -> dict:
        await d.require_csrf(request)
        ctx, membership = await d.owner_context(request, business_id)
        agent_limit.check(f"owner:{ctx.actor_id}")
        # a shop that is not live yet gets the business-starter guide, not shop analytics
        audience = "onboarding" if membership["status"] == "ONBOARDING" else "owner"
        return await d.call(
            d.agent,
            "POST",
            path,
            ctx,
            json_body={**body, "audience": audience, "unified": guide_only},
        )

    @app.post("/api/v1/admin/businesses/{business_id}/agent")
    async def owner_ask(business_id: str, body: AskBody, request: Request, response: Response):
        d.no_store(response)
        return await owner_agent(business_id, request, "/internal/v1/agent/ask", body.model_dump())

    @app.post("/api/v1/admin/businesses/{business_id}/business-guide")
    async def business_guide(business_id: str, body: AskBody, request: Request, response: Response):
        d.no_store(response)
        return await owner_agent(
            business_id, request, "/internal/v1/agent/ask", body.model_dump(), guide_only=True
        )

    # ---- step 14: platform administration --------------------------------------------------------
    async def platform_ctx(request: Request) -> InternalContext:
        session = await d.resolve_session(request)
        if session["kind"] != "owner":
            raise ApiError(401, "authentication_required", "Sign in first.")
        if (session["owner"] or {}).get("platform_role") != "admin":
            raise ApiError(403, "forbidden", "Platform administrators only.")
        return InternalContext(
            request_id=d.request_id(), actor_type="PLATFORM_ADMIN", actor_id=session["owner"]["id"]
        )

    async def platform(
        request: Request, method: str, path: str, body: dict | None = None, params=None
    ):
        if method == "GET":
            d.check_origin(request, mutation=False)
        else:
            await d.require_csrf(request)
        ctx = await platform_ctx(request)
        return await d.call(d.identity, method, path, ctx, params, body)

    @app.get("/api/v1/platform/summary")
    async def p_summary(request: Request, response: Response):
        d.no_store(response)
        data = await platform(request, "GET", "/internal/v1/platform/summary")
        services = {}
        for name in ("identity", "store", "catalog", "supply", "sales", "billing", "agent"):
            try:
                r = await getattr(d, name).request("GET", "/internal/v1/health", d.gateway_ctx())
                services[name] = "ready" if r.status_code == 200 else f"error {r.status_code}"
            except ApiError:
                services[name] = "down"
        return {**data, "services": services}

    @app.get("/api/v1/platform/businesses")
    async def p_businesses(request: Request, response: Response):
        d.no_store(response)
        params = {k: request.query_params[k] for k in ("q", "status") if k in request.query_params}
        return await platform(
            request, "GET", "/internal/v1/platform/businesses", params=params or None
        )

    @app.post("/api/v1/platform/businesses/{business_id}/suspend")
    async def p_suspend(business_id: str, body: SuspendBody, request: Request, response: Response):
        if not SAFE_ID.match(business_id):
            raise ApiError(404, "not_found", "Not found.")
        d.no_store(response)
        result = await platform(
            request,
            "POST",
            f"/internal/v1/platform/businesses/{business_id}/suspend",
            body.model_dump(),
        )
        d.forget_all_tenants()  # the shop must disappear now, not after the cache expires
        return result

    @app.post("/api/v1/platform/businesses/{business_id}/reinstate")
    async def p_reinstate(business_id: str, request: Request, response: Response):
        if not SAFE_ID.match(business_id):
            raise ApiError(404, "not_found", "Not found.")
        d.no_store(response)
        result = await platform(
            request, "POST", f"/internal/v1/platform/businesses/{business_id}/reinstate", {}
        )
        d.forget_all_tenants()
        return result

    @app.get("/api/v1/platform/audit")
    async def p_audit(request: Request, response: Response):
        d.no_store(response)
        params = {
            k: request.query_params[k]
            for k in ("business_id", "limit")
            if k in request.query_params
        }
        return await platform(request, "GET", "/internal/v1/platform/audit", params=params or None)
