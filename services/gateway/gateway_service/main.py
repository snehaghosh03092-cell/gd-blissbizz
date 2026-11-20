"""The gateway is the only service a browser talks to (docs/02 sections 1, 7, 8).

It owns cookies, CSRF and origin checks, resolves sessions through the identity service, builds
the signed context (business, actor, role, permissions) from a *verified* membership, and calls
services with signed requests. It has no database and no business rules.
"""

from __future__ import annotations

import hmac
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from blissbiz_common.clients import InternalClient, raise_for
from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError, error_body, register_exception_handlers
from blissbiz_common.logging import configure_logging, current_request_id, install_request_id
from blissbiz_common.permissions import permissions_for
from fastapi import FastAPI, Query, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, SecretStr

from gateway_service.admin_orders import install_admin_order_routes
from gateway_service.admin_proxy import install_admin_proxy
from gateway_service.extras import install_extra_routes
from gateway_service.storefront import install_storefront_routes

DEV_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://admin.localhost:5173",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://admin.localhost:8000",
    "http://platform.localhost:5173",
    "http://platform.localhost:8000",
]
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(self)",
}


class GatewaySettings(ServiceSettings):
    service_name: str = "gateway"
    identity_url: str = "http://127.0.0.1:8101"
    catalog_url: str = "http://127.0.0.1:8103"
    supply_url: str = "http://127.0.0.1:8104"
    store_url: str = "http://127.0.0.1:8102"
    sales_url: str = "http://127.0.0.1:8105"
    billing_url: str = "http://127.0.0.1:8106"
    agent_url: str = "http://127.0.0.1:8107"
    agent_rate_limit: int = Field(
        default=30, ge=1
    )  # assistant questions per minute per shopper IP / owner
    max_body_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    tenant_cache_seconds: float = Field(default=30, ge=0)  # host -> shop lookups
    allowed_origins: list[str] = Field(default_factory=lambda: list(DEV_ORIGINS))

    @property
    def cookie_name(self) -> str:
        return "__Host-blissbiz_session" if self.app_env == "production" else "blissbiz_session"


class LoginBody(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)


def _gateway_context() -> InternalContext:
    return InternalContext(
        request_id=current_request_id() or "unknown-request",
        actor_type="SERVICE",
        actor_id="gateway",
    )


_raise_for = raise_for  # downstream errors keep their code, message and fields


def create_app(
    settings: GatewaySettings | None = None,
    identity_transport: httpx.AsyncBaseTransport | None = None,
    catalog_transport: httpx.AsyncBaseTransport | None = None,
    supply_transport: httpx.AsyncBaseTransport | None = None,
    store_transport: httpx.AsyncBaseTransport | None = None,
    sales_transport: httpx.AsyncBaseTransport | None = None,
    billing_transport: httpx.AsyncBaseTransport | None = None,
    agent_transport: httpx.AsyncBaseTransport | None = None,
    configure_logs: bool = False,
) -> FastAPI:
    settings = settings or GatewaySettings()
    if configure_logs:
        configure_logging(settings.service_name, settings.log_level)
    secret = settings.internal_signing_secret.get_secret_value()
    identity = InternalClient(settings.identity_url, secret, transport=identity_transport)
    catalog = InternalClient(settings.catalog_url, secret, transport=catalog_transport)
    supply = InternalClient(settings.supply_url, secret, transport=supply_transport)
    store = InternalClient(settings.store_url, secret, transport=store_transport)
    sales = InternalClient(settings.sales_url, secret, transport=sales_transport)
    billing = InternalClient(settings.billing_url, secret, transport=billing_transport)
    agent = InternalClient(settings.agent_url, secret, transport=agent_transport, timeout=40.0)
    allowed = set(settings.allowed_origins)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        for client in (identity, catalog, supply, store, sales, billing, agent):
            await client.aclose()

    app = FastAPI(
        title="BlissBiz gateway",
        lifespan=lifespan,
        docs_url=None,
        openapi_url="/api/openapi.json",
    )
    install_request_id(app)
    register_exception_handlers(app)

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(request: Request):
        nonce = secrets.token_urlsafe(32)
        root_path = request.scope.get("root_path", "").rstrip("/")
        html = get_swagger_ui_html(
            openapi_url=f"{root_path}{app.openapi_url}", title=f"{app.title} - Swagger UI"
        ).body.decode("utf-8")
        return HTMLResponse(
            html.replace("<script", f'<script nonce="{nonce}"'),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; "
                    f"script-src 'nonce-{nonce}'; "
                    "style-src https://cdn.jsdelivr.net 'unsafe-inline'; "
                    "img-src 'self' data: https://fastapi.tiangolo.com; "
                    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
                ),
            },
        )

    @app.middleware("http")
    async def harden(request: Request, call_next):
        """Refuse oversized bodies before reading them; add browser security headers everywhere."""
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_body_bytes:
            response = JSONResponse(
                error_body("payload_too_large", "The request is too large."), status_code=413
            )
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    # ---- security helpers ---------------------------------------------------
    def check_origin(request: Request, *, mutation: bool) -> None:
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ApiError(403, "csrf_rejected", "Request verification failed.")
        source = request.headers.get("origin")
        if source is None and request.headers.get("referer"):
            parts = urlsplit(request.headers["referer"])
            source = f"{parts.scheme}://{parts.netloc}"
        same_origin = False
        if source is not None:
            parts = urlsplit(source)
            same_origin = parts.scheme in ("http", "https") and parts.netloc == request.headers.get(
                "host"
            )
        if (source is None and mutation) or (
            source is not None and source not in allowed and not same_origin
        ):
            raise ApiError(403, "csrf_rejected", "Request verification failed.")

    def token_of(request: Request) -> str | None:
        return request.cookies.get(settings.cookie_name)

    async def resolve_session(request: Request) -> dict:
        token = token_of(request)
        if not token:
            return {"kind": "none", "businesses": []}
        r = await identity.request(
            "POST",
            "/internal/v1/sessions/resolve",
            _gateway_context(),
            json_body={"raw_token": token},
        )
        return _raise_for(r) or {"kind": "none", "businesses": []}

    def verify_csrf(request: Request, session: dict) -> None:
        supplied = request.headers.get("x-csrf-token", "")
        expected = session.get("csrf_token") or ""
        if session["kind"] == "none" or not expected or not hmac.compare_digest(expected, supplied):
            raise ApiError(403, "csrf_rejected", "Request verification failed.")

    async def require_csrf(request: Request) -> dict:
        check_origin(request, mutation=True)
        session = await resolve_session(request)
        verify_csrf(request, session)
        return session

    def set_cookie(response: Response, data: dict) -> None:
        expires = datetime.fromisoformat(data["expires_at"]).replace(tzinfo=UTC)
        response.set_cookie(
            settings.cookie_name, data["raw_token"], max_age=data["max_age"], expires=expires,
            path="/", httponly=True, secure=settings.app_env == "production", samesite="lax",
        )  # fmt: skip

    def delete_session_cookie(response: Response) -> None:
        response.delete_cookie(settings.cookie_name, path="/", httponly=True,
                               secure=settings.app_env == "production", samesite="lax")  # fmt: skip

    def no_store(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Cookie, Origin"

    async def owner_context(request: Request, business_id: str) -> tuple[InternalContext, dict]:
        """Trusted context for a business-scoped call: a proven membership, never client input."""
        session = await resolve_session(request)
        if session["kind"] != "owner":
            raise ApiError(401, "authentication_required", "A valid owner session is required.")
        membership = next((b for b in session["businesses"] if b["id"] == business_id), None)
        if membership is None:
            raise ApiError(404, "not_found", "Not found.")  # other tenants look nonexistent
        if membership["status"] == "SUSPENDED":
            raise ApiError(403, "business_suspended", "This business is suspended.")
        ctx = InternalContext(
            request_id=current_request_id() or "unknown-request",
            actor_type="OWNER",
            business_id=business_id,
            actor_id=session["owner"]["id"],
            role=membership["role"],
            permissions=permissions_for(membership["role"]),
        )
        return ctx, membership

    app.state.owner_context = owner_context  # later admin routes proxy with this context
    app.state.identity = identity

    # ---- operational --------------------------------------------------------
    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/ready")
    async def ready():
        down = []
        services = (("identity", identity), ("store", store), ("catalog", catalog),
                    ("supply", supply), ("sales", sales), ("billing", billing),
                    ("agent", agent))  # fmt: skip
        for name, client in services:
            try:
                _raise_for(await client.request("GET", "/internal/v1/health", _gateway_context()))
            except ApiError:
                down.append(name)
        if down:
            raise ApiError(503, "service_unavailable", f"Not ready: {', '.join(down)}.")
        return {"status": "ready"}

    # ---- auth ---------------------------------------------------------------
    @app.get("/api/v1/auth/csrf")
    async def csrf(request: Request, response: Response):
        check_origin(request, mutation=False)
        r = await identity.request(
            "POST",
            "/internal/v1/auth/session",
            _gateway_context(),
            json_body={"raw_token": token_of(request)},
        )
        data = _raise_for(r)
        set_cookie(response, data)
        no_store(response)
        return {"csrf_token": data["csrf_token"]}

    @app.post("/api/v1/auth/login")
    async def login(body: LoginBody, request: Request, response: Response):
        await require_csrf(request)
        r = await identity.request(
            "POST",
            "/internal/v1/auth/login",
            _gateway_context(),
            json_body={
                "raw_token": token_of(request),
                "email": body.email,
                "password": body.password.get_secret_value(),
                "client_ip": request.client.host if request.client else "unknown",
            },
        )
        data = _raise_for(r)
        set_cookie(response, data)
        no_store(response)
        return {
            "csrf_token": data["csrf_token"],
            "owner": data["owner"],
            "businesses": data["businesses"],
        }

    @app.get("/api/v1/auth/me")
    async def me(request: Request, response: Response):
        check_origin(request, mutation=False)
        session = await resolve_session(request)
        no_store(response)
        if session["kind"] != "owner":
            raise ApiError(401, "authentication_required", "A valid owner session is required.")
        return {"owner": session["owner"], "businesses": session["businesses"]}

    @app.post("/api/v1/auth/logout", status_code=204)
    async def logout(request: Request):
        await require_csrf(request)
        _raise_for(
            await identity.request(
                "POST",
                "/internal/v1/auth/logout",
                _gateway_context(),
                json_body={"raw_token": token_of(request)},
            )
        )
        out = Response(status_code=204)
        delete_session_cookie(out)
        out.headers["Cache-Control"] = "no-store"
        return out

    # ---- admin: businesses the signed-in owner may act in ----------------------
    @app.get("/api/v1/admin/businesses")
    async def my_businesses(request: Request, response: Response):
        check_origin(request, mutation=False)
        session = await resolve_session(request)
        no_store(response)
        if session["kind"] != "owner":
            raise ApiError(401, "authentication_required", "A valid owner session is required.")
        return {"items": session["businesses"]}

    @app.get("/api/v1/admin/businesses/{business_id}")
    async def my_business(business_id: str, request: Request, response: Response):
        check_origin(request, mutation=False)
        ctx, membership = await owner_context(request, business_id)
        no_store(response)
        return {**membership, "permissions": list(ctx.permissions)}

    # ---- admin: catalog and stock (proxied with the proven business context) ----------
    async def call(
        client: InternalClient,
        method: str,
        path: str,
        ctx: InternalContext,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        response = await client.request(method, path, ctx, params=params, json_body=json_body)
        return _raise_for(response) or {}

    def passthrough(request: Request, *names: str) -> dict:
        return {k: request.query_params[k] for k in names if k in request.query_params}

    @app.get("/api/v1/admin/businesses/{business_id}/categories")
    async def categories(business_id: str, request: Request, response: Response):
        check_origin(request, mutation=False)
        ctx, _ = await owner_context(request, business_id)
        no_store(response)
        return await call(catalog, "GET", "/internal/v1/categories", ctx)

    @app.get("/api/v1/admin/businesses/{business_id}/products")
    async def products(business_id: str, request: Request, response: Response):
        check_origin(request, mutation=False)
        ctx, _ = await owner_context(request, business_id)
        no_store(response)
        params = passthrough(request, "status", "category", "q", "page", "page_size")
        return await call(catalog, "GET", "/internal/v1/products", ctx, params)

    @app.get("/api/v1/admin/businesses/{business_id}/products/{product_id}")
    async def product(business_id: str, product_id: str, request: Request, response: Response):
        check_origin(request, mutation=False)
        ctx, _ = await owner_context(request, business_id)
        no_store(response)
        detail = await call(catalog, "GET", f"/internal/v1/products/{product_id}", ctx)
        ids = [v["id"] for v in detail["variants"]][:200]
        try:  # stock is a nice-to-have here: if supply is down the catalog data still loads
            stock = {}
            if ids:
                query = {"variant_ids": ids}
                stock = (
                    await call(supply, "POST", "/internal/v1/stock/query", ctx, json_body=query)
                )["items"]
            detail["stock_unavailable"] = False
        except ApiError as exc:
            if exc.status != 503:
                raise
            stock, detail["stock_unavailable"] = {}, True
        for variant in detail["variants"]:
            variant["stock"] = stock.get(variant["id"])
        return detail

    @app.get("/api/v1/admin/businesses/{business_id}/warehouses")
    async def warehouses(business_id: str, request: Request, response: Response):
        check_origin(request, mutation=False)
        ctx, _ = await owner_context(request, business_id)
        no_store(response)
        return await call(supply, "GET", "/internal/v1/warehouses", ctx)

    async def all_variants(ctx: InternalContext, q: str | None) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            params = {"page": page, "page_size": 200, **({"q": q} if q else {})}
            data = await call(catalog, "GET", "/internal/v1/variants", ctx, params)
            items += data["items"]
            if not data["items"] or len(items) >= data["total"]:
                return items
            page += 1

    async def stock_for(ctx: InternalContext, variant_ids: list[str]) -> dict:
        merged: dict = {}
        for start in range(0, len(variant_ids), 200):
            chunk = variant_ids[start : start + 200]
            data = await call(supply, "POST", "/internal/v1/stock/query", ctx,
                              json_body={"variant_ids": chunk})  # fmt: skip
            merged.update(data["items"])
        return merged

    @app.get("/api/v1/admin/businesses/{business_id}/inventory")
    async def inventory(
        business_id: str,
        request: Request,
        response: Response,
        low_only: bool = False,
        out_of_stock: bool = False,
        q: str | None = Query(default=None, max_length=100),
        page: int = Query(default=1, ge=1, le=100000),
        page_size: int = Query(default=20, ge=1, le=100),
    ):
        """Stock with product names: joins catalog (what it is) with supply (how many we have).

        Status rules match the ``rpt_low_stock`` read view: only published, active variants can be
        LOW or OUT; LOW means available <= reorder threshold (OUT is the available <= 0 subset).
        """
        check_origin(request, mutation=False)
        ctx, _ = await owner_context(request, business_id)
        ctx.require_permission("catalog.read")
        ctx.require_permission("inventory.read")
        no_store(response)
        variants = await all_variants(ctx, q)
        stock = await stock_for(ctx, [v["variant_id"] for v in variants])
        rows = []
        for v in variants:
            s = stock.get(v["variant_id"]) or {"on_hand": "0", "reserved": "0", "available": "0",
                                                "warehouses": []}  # fmt: skip
            available, threshold = Decimal(s["available"]), Decimal(v["reorder_threshold"])
            sellable = v["product_status"] == "PUBLISHED" and v["is_active"]
            if not sellable:
                status = "NOT_FOR_SALE"
            elif available <= 0:
                status = "OUT"
            elif available <= threshold:
                status = "LOW"
            else:
                status = "OK"
            stock_fields = {k: s[k] for k in ("on_hand", "reserved", "available", "warehouses")}
            rows.append({**v, **stock_fields, "status": status})
        summary = {
            "variants": len(rows),
            "out_of_stock": sum(r["status"] == "OUT" for r in rows),
            "low_stock": sum(r["status"] in ("OUT", "LOW") for r in rows),
        }
        if out_of_stock:
            rows = [r for r in rows if r["status"] == "OUT"]
        elif low_only:
            rows = [r for r in rows if r["status"] in ("OUT", "LOW")]
        total = len(rows)
        start = (page - 1) * page_size
        return {"items": rows[start : start + page_size], "page": page, "page_size": page_size,
                "total": total, "summary": summary}  # fmt: skip

    @app.get("/api/v1/admin/businesses/{business_id}/inventory/ledger")
    async def ledger(business_id: str, request: Request, response: Response):
        check_origin(request, mutation=False)
        ctx, _ = await owner_context(request, business_id)
        no_store(response)
        params = passthrough(request, "variant_id", "page", "page_size")
        return await call(supply, "GET", "/internal/v1/stock/ledger", ctx, params)

    deps = SimpleNamespace(
        settings=settings, identity=identity, catalog=catalog, supply=supply, store=store,
        sales=sales, billing=billing, agent=agent, call=call, passthrough=passthrough,
        check_origin=check_origin,
        resolve_session=resolve_session, verify_csrf=verify_csrf, require_csrf=require_csrf,
        owner_context=owner_context, set_cookie=set_cookie, no_store=no_store,
        delete_session_cookie=delete_session_cookie, token_of=token_of,
        gateway_ctx=_gateway_context, request_id=lambda: current_request_id() or "unknown-request",
        permissions_for=permissions_for,
    )  # fmt: skip
    install_storefront_routes(app, deps)
    install_admin_order_routes(app, deps)
    install_admin_proxy(app, deps)
    install_extra_routes(app, deps)
    return app


app = create_app(configure_logs=True)
