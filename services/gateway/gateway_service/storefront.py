"""Shopper API: ``/api/v1/storefront/...`` (docs/05 section 4).

The shop is chosen by the **Host header** (``threads-co.localhost``), never by an id from the
browser: store resolves the hostname (ACTIVE domains only), the storefront must be PUBLISHED and
the business ACTIVE, otherwise the answer is 404 ``store_not_found``.

The signed context a service sees is CUSTOMER only when the cookie belongs to a customer of
*this* shop; anything else (no cookie, an owner cookie, another shop's customer) is ANONYMOUS.
Mutations need the CSRF token, and the Origin must be this same shop.
"""

import re
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

from blissbiz_common.cache import TTLCache
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import Body, FastAPI, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

LOW_STOCK = Decimal(5)  # "only N left" below this; exact counts above it only if the shop allows
CART_COOKIE_DAYS = 30
IDEMPOTENCY = re.compile(r"[A-Za-z0-9_.:-]{8,64}")
SORTS = ("relevance", "price_asc", "price_desc", "newest", "name")


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    password: SecretStr = Field(max_length=128)
    full_name: str = Field(max_length=150)
    phone: str | None = Field(default=None, max_length=32)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    password: SecretStr = Field(max_length=128)


class CartItemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant_id: str = Field(min_length=1, max_length=36)
    quantity: Decimal = Field(ge=0, le=99, max_digits=14, decimal_places=3)


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address_id: str | None = Field(default=None, max_length=36)


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address_id: str = Field(min_length=1, max_length=36)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=255)


def host_of(request: Request) -> str:
    host = (request.headers.get("host") or "").strip().lower()
    return "" if host.startswith("[") else host.split(":", 1)[0].rstrip(".")


def _qty(value: Decimal) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def availability(available: Decimal, show_exact: bool) -> dict:
    if available <= 0:
        band = "OUT_OF_STOCK"
    elif available <= LOW_STOCK:
        band = "LOW_STOCK"
    else:
        band = "IN_STOCK"
    out: dict = {"availability": band, "in_stock": available > 0}
    if available > 0 and (show_exact or band == "LOW_STOCK"):
        out["available_quantity"] = _qty(available)
    return out


def install_storefront_routes(app: FastAPI, d) -> None:
    """``d`` carries the gateway's clients and helpers (see main.create_app)."""
    s = d.settings
    tenants = TTLCache(s.tenant_cache_seconds)
    d.forget_tenant = lambda host: tenants.pop(host) if host else None
    d.forget_all_tenants = tenants.clear
    cart_cookie = "__Host-blissbiz_cart" if s.app_env == "production" else "blissbiz_cart"

    async def tenant(request: Request) -> dict:
        host = host_of(request)
        if not host:
            raise ApiError(404, "store_not_found", "There is no shop at this address.")
        cached = tenants.get(host)
        if cached is not None:
            return cached
        try:
            found = await d.call(
                d.store, "GET", "/internal/v1/hosts/resolve", d.gateway_ctx(), params={"host": host}
            )
        except ApiError as exc:
            if exc.status == 404:
                raise ApiError(
                    404, "store_not_found", "There is no shop at this address."
                ) from None
            raise
        if found["storefront_status"] != "PUBLISHED":
            raise ApiError(404, "store_not_found", "There is no shop at this address.")
        bid = found["business_id"]
        profile = await d.call(
            d.identity, "GET", f"/internal/v1/businesses/{bid}/profile", d.gateway_ctx()
        )
        if profile["status"] != "ACTIVE":
            raise ApiError(404, "store_not_found", "There is no shop at this address.")
        ctx = InternalContext(
            request_id=d.request_id(), actor_type="SERVICE", business_id=bid, actor_id="gateway"
        )
        policies = await d.call(d.store, "GET", "/internal/v1/policies", ctx)
        value = {"business_id": bid, "host": host, "profile": profile, "policies": policies}
        tenants.set(host, value)
        return value

    def check_origin(request: Request, t: dict, *, mutation: bool) -> None:
        """Same-shop requests only: the Origin (or Referer) must be this shop's hostname."""
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ApiError(403, "csrf_rejected", "Request verification failed.")
        source = request.headers.get("origin") or request.headers.get("referer")
        if source is None:
            if mutation:
                raise ApiError(403, "csrf_rejected", "Request verification failed.")
            return
        parts = urlsplit(source)
        if parts.scheme not in ("http", "https") or (parts.hostname or "") != t["host"]:
            raise ApiError(403, "csrf_rejected", "Request verification failed.")

    async def shopper(
        request: Request, *, mutation: bool = False
    ) -> tuple[InternalContext, dict, dict]:
        """(context, session, tenant) for a shopper request; mutations also verify CSRF."""
        t = await tenant(request)
        check_origin(request, t, mutation=mutation)
        session = await d.resolve_session(request)
        if mutation:
            d.verify_csrf(request, session)
        if session["kind"] == "customer" and session.get("business_id") == t["business_id"]:
            ctx = InternalContext(
                request_id=d.request_id(),
                actor_type="CUSTOMER",
                business_id=t["business_id"],
                actor_id=session["customer"]["id"],
            )
        else:
            ctx = InternalContext(
                request_id=d.request_id(), actor_type="ANONYMOUS", business_id=t["business_id"]
            )
        return ctx, session, t

    def set_cart_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            cart_cookie,
            token,
            max_age=CART_COOKIE_DAYS * 86400,
            path="/",
            httponly=True,
            secure=s.app_env == "production",
            samesite="lax",
        )

    def clear_cart_cookie(response: Response) -> None:
        response.delete_cookie(
            cart_cookie, path="/", httponly=True, secure=s.app_env == "production", samesite="lax"
        )

    async def merge_cart(request: Request, response: Response, ctx: InternalContext) -> None:
        """A signed-in customer still holding an anonymous cart cookie: fold it into their cart.
        If sales is down the cookie is kept and the merge is retried on the next cart call."""
        token = request.cookies.get(cart_cookie)
        if not token or ctx.actor_type != "CUSTOMER":
            return
        try:
            await d.call(
                d.sales, "POST", "/internal/v1/cart/merge", ctx, json_body={"cart_token": token}
            )
        except ApiError as exc:
            if exc.status >= 500:
                return
            raise
        clear_cart_cookie(response)

    def customer_ctx(t: dict, customer: dict) -> InternalContext:
        return InternalContext(
            request_id=d.request_id(),
            actor_type="CUSTOMER",
            business_id=t["business_id"],
            actor_id=customer["id"],
        )

    # ---- shop and catalog -------------------------------------------------------------
    @app.get("/api/v1/storefront")
    async def store_info(request: Request, response: Response):
        ctx, _, t = await shopper(request)
        sf = await d.call(d.store, "GET", "/internal/v1/storefront", ctx)
        p = t["profile"]
        d.no_store(response)
        business = {
            k: p.get(k)
            for k in (
                "name",
                "slug",
                "city",
                "state_code",
                "description",
                "contact_email",
                "contact_phone",
                "currency",
                "gst_registered",
                "prices_include_tax",
            )
        }
        return {"business": business, **sf}

    @app.get("/api/v1/storefront/categories")
    async def categories(request: Request, response: Response):
        ctx, _, _ = await shopper(request)
        d.no_store(response)
        return await d.call(d.catalog, "GET", "/internal/v1/public/categories", ctx)

    async def free_stock(ctx: InternalContext, ids: list[str]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for start in range(0, len(ids), 200):
            data = await d.call(
                d.supply,
                "POST",
                "/internal/v1/public/availability",
                ctx,
                json_body={"variant_ids": ids[start : start + 200]},
            )
            out.update({k: Decimal(v) for k, v in data["items"].items()})
        return out

    def present(product: dict, stock: dict, show_exact: bool) -> dict:
        variants = []
        for v in product["variants"]:
            variants.append({**v, **availability(stock.get(v["id"], Decimal(0)), show_exact)})
        prices = [Decimal(v["price"]) for v in variants]
        total = sum((stock.get(v["id"], Decimal(0)) for v in variants), Decimal(0))
        return {
            **product,
            "variants": variants,
            "price_min": f"{min(prices):.2f}" if prices else None,
            "price_max": f"{max(prices):.2f}" if prices else None,
            **availability(total, show_exact),
        }

    @app.get("/api/v1/storefront/products")
    async def products(
        request: Request,
        response: Response,
        q: str | None = Query(default=None, max_length=100),
        category: str | None = Query(default=None, max_length=100),
        color: str | None = Query(default=None, max_length=40),
        size: str | None = Query(default=None, max_length=40),
        min_price: Decimal | None = Query(default=None, ge=0, le=10_000_000),
        max_price: Decimal | None = Query(default=None, ge=0, le=10_000_000),
        in_stock: bool = False,
        sort: Literal[SORTS] = "relevance",  # type: ignore[valid-type]
        page: int = Query(default=1, ge=1, le=10000),
        page_size: int = Query(default=24, ge=1, le=100),
    ):
        """Search published products; stock comes from supply, so it is always current."""
        ctx, _, t = await shopper(request)
        params = {
            k: v
            for k, v in {
                "q": q,
                "category": category,
                "color": color,
                "size": size,
                "min_price": min_price,
                "max_price": max_price,
            }.items()
            if v is not None
        }
        found = (await d.call(d.catalog, "GET", "/internal/v1/public/products", ctx, params))[
            "items"
        ]
        stock = await free_stock(ctx, [v["id"] for p in found for v in p["variants"]])
        show_exact = bool(t["policies"].get("show_exact_stock"))
        items = [present(p, stock, show_exact) for p in found]
        if in_stock:
            items = [p for p in items if p["in_stock"]]
        if sort == "price_asc":
            items.sort(key=lambda p: (Decimal(p["price_min"]), p["name"]))
        elif sort == "price_desc":
            items.sort(key=lambda p: (-Decimal(p["price_max"]), p["name"]))
        elif sort == "newest":
            items.sort(key=lambda p: p["created_at"], reverse=True)
        elif sort == "name":
            items.sort(key=lambda p: p["name"].lower())
        else:  # relevance: in-stock first, then the catalog's order
            items.sort(key=lambda p: not p["in_stock"])
        d.no_store(response)
        start = (page - 1) * page_size
        return {
            "items": items[start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total": len(items),
        }

    @app.get("/api/v1/storefront/products/{slug}")
    async def product(slug: str, request: Request, response: Response):
        ctx, _, t = await shopper(request)
        p = await d.call(d.catalog, "GET", f"/internal/v1/public/products/{slug}", ctx)
        stock = await free_stock(ctx, [v["id"] for v in p["variants"]])
        d.no_store(response)
        return present(p, stock, bool(t["policies"].get("show_exact_stock")))

    # ---- customer accounts ---------------------------------------------------------------
    @app.get("/api/v1/storefront/auth/csrf")
    async def csrf(request: Request, response: Response):
        await shopper(request)
        data = await d.call(
            d.identity,
            "POST",
            "/internal/v1/auth/session",
            d.gateway_ctx(),
            json_body={"raw_token": d.token_of(request)},
        )
        d.set_cookie(response, data)
        d.no_store(response)
        return {"csrf_token": data["csrf_token"]}

    async def sign_in(path: str, payload: dict, request: Request, response: Response) -> dict:
        ctx, session, t = await shopper(request, mutation=True)
        if session["kind"] in ("customer", "owner"):
            raise ApiError(409, "already_authenticated", "Sign out before signing in again.")
        data = await d.call(
            d.identity,
            "POST",
            path,
            ctx,
            json_body={
                **payload,
                "raw_token": d.token_of(request),
                "client_ip": request.client.host if request.client else "unknown",
            },
        )
        d.set_cookie(response, data)
        await merge_cart(request, response, customer_ctx(t, data["customer"]))
        d.no_store(response)
        return {"csrf_token": data["csrf_token"], "customer": data["customer"]}

    @app.post("/api/v1/storefront/auth/register", status_code=201)
    async def register(body: RegisterBody, request: Request, response: Response):
        payload = body.model_dump(exclude={"password"})
        payload["password"] = body.password.get_secret_value()
        return await sign_in("/internal/v1/customers/register", payload, request, response)

    @app.post("/api/v1/storefront/auth/login")
    async def login(body: LoginBody, request: Request, response: Response):
        payload = {"email": body.email, "password": body.password.get_secret_value()}
        try:
            return await sign_in("/internal/v1/customers/login", payload, request, response)
        except ApiError as exc:
            if exc.status != 401 or exc.code != "invalid_credentials":
                raise

        # The same form also accepts an owner/staff account for this exact tenant. Owners keep an
        # owner session (never a customer identity), so cart/order authorization remains isolated.
        _, session, t = await shopper(request, mutation=True)
        if session["kind"] in ("customer", "owner"):
            raise ApiError(409, "already_authenticated", "Sign out before signing in again.")
        data = await d.call(
            d.identity,
            "POST",
            "/internal/v1/auth/storefront-owner-login",
            d.gateway_ctx(),
            json_body={
                **payload,
                "business_id": t["business_id"],
                "raw_token": d.token_of(request),
                "client_ip": request.client.host if request.client else "unknown",
            },
        )
        membership = next(b for b in data["businesses"] if b["id"] == t["business_id"])
        d.set_cookie(response, data)
        d.no_store(response)
        return {
            "csrf_token": data["csrf_token"],
            "owner": data["owner"],
            "business": membership,
        }

    @app.post("/api/v1/storefront/auth/logout", status_code=204)
    async def logout(request: Request):
        await shopper(request, mutation=True)
        await d.call(
            d.identity,
            "POST",
            "/internal/v1/auth/logout",
            d.gateway_ctx(),
            json_body={"raw_token": d.token_of(request)},
        )
        out = Response(status_code=204)
        d.delete_session_cookie(out)
        out.headers["Cache-Control"] = "no-store"
        return out

    @app.get("/api/v1/storefront/auth/me")
    async def me(request: Request, response: Response):
        ctx, session, t = await shopper(request)
        d.no_store(response)
        if ctx.actor_type == "CUSTOMER":
            return {"actor_type": "customer", "customer": session["customer"]}
        if session["kind"] == "owner":
            membership = next(
                (b for b in session["businesses"] if b["id"] == t["business_id"]), None
            )
            if membership is not None and membership["status"] == "ACTIVE":
                return {
                    "actor_type": "owner",
                    "owner": session["owner"],
                    "business": membership,
                }
        raise ApiError(401, "authentication_required", "Please sign in.")

    # ---- addresses -----------------------------------------------------------------------
    @app.get("/api/v1/storefront/me/addresses")
    async def addresses(request: Request, response: Response):
        ctx, _, _ = await shopper(request)
        d.no_store(response)
        return await d.call(d.identity, "GET", "/internal/v1/customers/me/addresses", ctx)

    @app.post("/api/v1/storefront/me/addresses", status_code=201)
    async def add_address(request: Request, response: Response, body: dict[str, Any] = Body(...)):
        ctx, _, _ = await shopper(request, mutation=True)
        d.no_store(response)  # identity validates every field (unknown fields are rejected)
        return await d.call(
            d.identity, "POST", "/internal/v1/customers/me/addresses", ctx, json_body=body
        )

    @app.delete("/api/v1/storefront/me/addresses/{address_id}", status_code=204)
    async def delete_address(address_id: str, request: Request):
        ctx, _, _ = await shopper(request, mutation=True)
        await d.call(d.identity, "DELETE", f"/internal/v1/customers/me/addresses/{address_id}", ctx)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    # ---- cart ------------------------------------------------------------------------------
    @app.get("/api/v1/storefront/cart")
    async def cart(request: Request, response: Response):
        ctx, _, _ = await shopper(request)
        await merge_cart(request, response, ctx)
        d.no_store(response)
        token = None if ctx.actor_type == "CUSTOMER" else request.cookies.get(cart_cookie)
        return await d.call(
            d.sales, "POST", "/internal/v1/cart/get", ctx, json_body={"cart_token": token}
        )

    @app.post("/api/v1/storefront/cart/items")
    async def cart_item(body: CartItemBody, request: Request, response: Response):
        ctx, _, _ = await shopper(request, mutation=True)
        await merge_cart(request, response, ctx)
        token = None if ctx.actor_type == "CUSTOMER" else request.cookies.get(cart_cookie)
        data = await d.call(
            d.sales,
            "POST",
            "/internal/v1/cart/items",
            ctx,
            json_body={
                "cart_token": token,
                "variant_id": body.variant_id,
                "quantity": str(body.quantity),
            },
        )
        new_token = data.pop("cart_token", None)
        if new_token:
            set_cart_cookie(response, new_token)
        d.no_store(response)
        return data

    # ---- checkout (cash on delivery) and orders ------------------------------------------
    async def ship_to(ctx: InternalContext, session: dict, address_id: str) -> dict:
        a = await d.call(
            d.identity, "GET", f"/internal/v1/customers/me/addresses/{address_id}", ctx
        )
        phone = a.get("phone") or (session.get("customer") or {}).get("phone")
        if not phone:
            raise ApiError(
                422,
                "validation_error",
                "Add a phone number to this address.",
                {"address_id": "A phone number is needed for delivery."},
            )
        return {
            "recipient": a["recipient"],
            "phone": phone,
            "line1": a["line1"],
            "line2": a.get("line2"),
            "city": a["city"],
            "state_code": a["state_code"],
            "postal_code": a["postal_code"],
        }

    def require_customer(ctx: InternalContext) -> None:
        if ctx.actor_type != "CUSTOMER":
            raise ApiError(401, "authentication_required", "Please sign in to continue.")

    @app.post("/api/v1/storefront/checkout/preview")
    async def preview(body: PreviewBody, request: Request, response: Response):
        ctx, session, _ = await shopper(request, mutation=True)
        require_customer(ctx)
        state = (
            (await ship_to(ctx, session, body.address_id))["state_code"]
            if body.address_id
            else None
        )
        d.no_store(response)
        return await d.call(
            d.sales,
            "POST",
            "/internal/v1/checkout/preview",
            ctx,
            json_body={"ship_to_state_code": state},
        )

    @app.post("/api/v1/storefront/checkout", status_code=201)
    async def checkout(
        body: CheckoutBody,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        """Place a cash-on-delivery order. Send the same Idempotency-Key when retrying: the same
        order comes back instead of a second one."""
        if not idempotency_key or not IDEMPOTENCY.fullmatch(idempotency_key):
            raise ApiError(
                400,
                "idempotency_key_required",
                "Send an Idempotency-Key header (8-64 letters, digits, '-', '_', '.', ':').",
            )
        ctx, session, _ = await shopper(request, mutation=True)
        require_customer(ctx)
        address = await ship_to(ctx, session, body.address_id)
        data = await d.call(
            d.sales,
            "POST",
            "/internal/v1/checkout",
            ctx,
            json_body={"idempotency_key": idempotency_key, "ship_to": address},
        )
        if data.get("replayed"):
            response.status_code = 200
        d.no_store(response)
        return data

    @app.get("/api/v1/storefront/orders")
    async def my_orders(
        request: Request,
        response: Response,
        page: int = Query(default=1, ge=1, le=10000),
        page_size: int = Query(default=20, ge=1, le=100),
    ):
        ctx, _, _ = await shopper(request)
        d.no_store(response)
        return await d.call(
            d.sales, "GET", "/internal/v1/my/orders", ctx, {"page": page, "page_size": page_size}
        )

    @app.get("/api/v1/storefront/orders/{order_id}")
    async def my_order(order_id: str, request: Request, response: Response):
        ctx, _, _ = await shopper(request)
        d.no_store(response)
        return await d.call(d.sales, "GET", f"/internal/v1/my/orders/{order_id}", ctx)

    # ---- the shop assistant (typed text or browser Web Speech transcript) ---------------------
    @app.post("/api/v1/storefront/agent")
    async def ask(request: Request, response: Response, body: dict[str, Any] = Body(...)):
        """{"text": "black dresses under 1500"} -> answer + product cards.

        Read-only and rate-limited per visitor."""
        ctx, _, _ = await shopper(request, mutation=True)
        d.agent_limit.check(f"shop:{request.client.host if request.client else 'unknown'}")
        d.no_store(response)
        payload = {k: body.get(k) for k in ("text", "conversation_id") if body.get(k) is not None}
        return await d.call(
            d.agent,
            "POST",
            "/internal/v1/agent/ask",
            ctx,
            json_body={**payload, "audience": "shopper"},
        )

    @app.get("/api/v1/storefront/orders/{order_id}/invoice")
    async def my_invoice(order_id: str, request: Request, response: Response):
        ctx, _, _ = await shopper(request)
        d.no_store(response)
        return await d.call(d.sales, "GET", f"/internal/v1/my/orders/{order_id}/invoice", ctx)

    @app.post("/api/v1/storefront/orders/{order_id}/returns", status_code=201)
    async def my_return(
        order_id: str, request: Request, response: Response, body: dict[str, Any] = Body(...)
    ):
        """Ask to return delivered items.

        Body: {"reason": "...", "items": [{"order_item_id": "...", "quantity": 1}]}."""
        ctx, _, _ = await shopper(request, mutation=True)
        d.no_store(response)
        return await d.call(
            d.sales, "POST", f"/internal/v1/my/orders/{order_id}/returns", ctx, json_body=body
        )

    @app.post("/api/v1/storefront/orders/{order_id}/cancel")
    async def my_cancel(order_id: str, body: CancelBody, request: Request, response: Response):
        ctx, _, _ = await shopper(request, mutation=True)
        d.no_store(response)
        return await d.call(
            d.sales,
            "POST",
            f"/internal/v1/my/orders/{order_id}/cancel",
            ctx,
            json_body=body.model_dump(),
        )
