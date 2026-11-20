"""Cart and cash-on-delivery checkout (docs/04 section 5, docs/06).

Cart: an anonymous shopper's cart is found by the SHA-256 of a random cookie token (the raw
token lives only in the browser); a signed-in customer's cart is found by customer id. Carts
store only variant + quantity; prices are always re-read from the catalog.

Checkout (customer only, ``idempotency_key`` required):
  1. same key again -> the order already placed (no second order, no second reservation)
  2. price the cart from the catalog, shipping from store policy, GST from billing
  3. reserve stock in supply (all-or-nothing; 409 out_of_stock names the variants)
  4. write order + lines + history in one local transaction; cart -> CONVERTED
  If step 4 fails, the reservation from step 3 is released (compensation).
"""

import hashlib
import logging
import re
import secrets
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from app.db.models._common import new_id, utc_now
from app.db.models.sales import Cart, CartItem, Order, OrderItem, OrderStatusHistory
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

log = logging.getLogger("sales")
ZERO = Decimal(0)
FRACTIONAL_UNITS = {"KG", "L"}  # everything else is sold in whole units
MAX_LINES, MAX_QTY = 50, Decimal(99)
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
NUMBER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I confusion
IST = timedelta(hours=5, minutes=30)
Qty = Annotated[Decimal, Field(ge=0, le=MAX_QTY, max_digits=14, decimal_places=3)]


class CartRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cart_token: str | None = Field(default=None, max_length=64)


class CartItemIn(CartRef):
    variant_id: str = Field(min_length=1, max_length=36)
    quantity: Qty


class ShipTo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: Annotated[str, StringConstraints(min_length=1, max_length=150)]
    phone: Annotated[str, StringConstraints(min_length=6, max_length=32)]
    line1: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    line2: Annotated[str, StringConstraints(max_length=200)] | None = None
    city: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    state_code: str = Field(pattern=r"^[0-9]{2}$")
    postal_code: str = Field(pattern=r"^[1-9][0-9]{5}$")


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ship_to_state_code: str | None = Field(default=None, pattern=r"^[0-9]{2}$")


class CheckoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,64}$")
    ship_to: ShipTo


def money(value: Decimal) -> str:
    return f"{value:.2f}"


def qty_str(value: Decimal) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def scoped_key(customer_id: str, key: str) -> str:
    """Idempotency keys are per customer: two shoppers who pick the same key never collide."""
    return hashlib.sha256(f"{customer_id}:{key}".encode()).hexdigest()


def order_number(prefix: str) -> str:
    day = (utc_now() + IST).strftime("%y%m%d")
    return f"{prefix}-{day}-{''.join(secrets.choice(NUMBER_ALPHABET) for _ in range(5))}"


def order_out(o: Order, items: list[OrderItem] | None = None, **extra) -> dict:
    out = {
        "id": o.id,
        "order_number": o.order_number,
        "order_status": o.order_status,
        "payment_status": o.payment_status,
        "fulfilment_status": o.fulfilment_status,
        "payment_method": o.payment_method,
        "currency": o.currency,
        "subtotal": money(o.subtotal),
        "discount_total": money(o.discount_total),
        "tax_total": money(o.tax_total),
        "shipping_total": money(o.shipping_total),
        "round_off": money(o.round_off),
        "grand_total": money(o.grand_total),
        "ship_to": o.ship_to,
        "place_of_supply_state": o.place_of_supply_state,
        "placed_at": o.placed_at.isoformat(),
        "customer_id": o.customer_id,
        "cancelled_at": o.cancelled_at.isoformat() if o.cancelled_at else None,
        "cancel_reason": o.cancel_reason,
    }
    if items is not None:
        out["items"] = [
            {
                "id": i.id,
                "product_id": i.product_id,
                "variant_id": i.variant_id,
                "product_name": i.product_name,
                "sku": i.sku,
                "options": i.options,
                "hsn_sac": i.hsn_sac,
                "stock_unit": i.stock_unit,
                "quantity": qty_str(i.quantity),
                "unit_price": money(i.unit_price),
                "discount": money(i.discount),
                "taxable_value": money(i.taxable_value),
                "cgst": money(i.cgst),
                "sgst": money(i.sgst),
                "igst": money(i.igst),
                "cess": money(i.cess),
                "tax_class": i.tax_class,
                "line_total": money(i.line_total),
            }
            for i in items
        ]
    return {**out, **extra}


def install_shop_routes(app: FastAPI, *, Ctx, Db, remote, settings) -> None:
    def business(ctx: InternalContext) -> str:
        if ctx.business_id is None:
            raise ApiError(404, "not_found", "Not found.")
        return ctx.business_id

    def customer_of(ctx: InternalContext) -> str | None:
        return ctx.actor_id if ctx.actor_type == "CUSTOMER" and ctx.actor_id else None

    def require_customer(ctx: InternalContext) -> tuple[str, str]:
        b, customer = business(ctx), customer_of(ctx)
        if customer is None:
            raise ApiError(401, "authentication_required", "Please sign in to continue.")
        return b, customer

    # ---- cart lookup -----------------------------------------------------------------
    def find_cart(db: Session, ctx: InternalContext, cart_token: str | None) -> Cart | None:
        b, customer = business(ctx), customer_of(ctx)
        query = select(Cart).where(Cart.business_id == b, Cart.status == "ACTIVE")
        if customer:
            query = query.where(Cart.customer_id == customer)
        elif cart_token and TOKEN.fullmatch(cart_token):
            query = query.where(
                Cart.customer_id.is_(None), Cart.anonymous_token_hash == token_hash(cart_token)
            )
        else:
            return None
        return db.scalar(query.order_by(Cart.created_at.desc()).limit(1))

    def items_of(db: Session, cart: Cart | None) -> list[CartItem]:
        if cart is None:
            return []
        return list(
            db.scalars(select(CartItem).where(CartItem.cart_id == cart.id).order_by(CartItem.id))
        )

    def facts(ctx: InternalContext, variant_ids: list[str]) -> tuple[dict, dict]:
        """(catalog facts, available quantity) for variants, from catalog and supply."""
        if not variant_ids:
            return {}, {}
        sctx = remote.service_ctx(ctx)
        quoted = remote.call(
            "catalog",
            "POST",
            "/internal/v1/public/quote",
            sctx,
            json_body={"variant_ids": variant_ids},
        )["items"]
        free = remote.call(
            "supply",
            "POST",
            "/internal/v1/public/availability",
            sctx,
            json_body={"variant_ids": variant_ids},
        )["items"]
        return quoted, {k: Decimal(v) for k, v in free.items()}

    def cart_view(ctx: InternalContext, items: list[CartItem], **extra) -> dict:
        quoted, free = facts(ctx, [i.variant_id for i in items])
        lines, subtotal, problems = [], ZERO, 0
        for item in items:
            info = quoted.get(item.variant_id)
            available = free.get(item.variant_id, ZERO)
            if info is None or not info["sellable"]:
                issue = "unavailable"
            elif item.quantity > available:
                issue = "insufficient_stock"
            else:
                issue = None
            line_total = (
                (Decimal(info["price"]) * item.quantity).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                if info
                else ZERO
            )
            if issue is None:
                subtotal += line_total
            problems += issue is not None
            lines.append(
                {
                    "variant_id": item.variant_id,
                    "quantity": qty_str(item.quantity),
                    "product_name": info["product_name"] if info else None,
                    "product_slug": info["product_slug"] if info else None,
                    "sku": info["sku"] if info else None,
                    "options": info["options"] if info else {},
                    "unit_price": info["price"] if info else None,
                    "stock_unit": info["stock_unit"] if info else None,
                    "line_total": money(line_total),
                    "available": qty_str(available),
                    "issue": issue,
                }
            )
        return {
            "items": lines,
            "item_count": len(lines),
            "subtotal": money(subtotal),
            "has_issues": problems > 0,
            **extra,
        }

    # ---- cart endpoints ----------------------------------------------------------------
    @app.post("/internal/v1/cart/get")
    def get_cart(body: CartRef, ctx: Ctx, db: Db):
        return cart_view(ctx, items_of(db, find_cart(db, ctx, body.cart_token)))

    @app.post("/internal/v1/cart/items")
    def set_item(body: CartItemIn, ctx: Ctx, db: Db):
        """Set the quantity of one variant (0 removes it). Returns the whole cart."""
        b = business(ctx)
        cart = find_cart(db, ctx, body.cart_token)
        new_token = None
        if body.quantity > 0:  # validate before writing anything (remote calls first)
            quoted, free = facts(ctx, [body.variant_id])
            info = quoted.get(body.variant_id)
            if info is None or not info["sellable"]:
                raise ApiError(404, "not_found", "This item is not available.")
            if (
                info["stock_unit"] not in FRACTIONAL_UNITS
                and body.quantity != body.quantity.to_integral_value()
            ):
                raise ApiError(
                    422,
                    "validation_error",
                    "Choose a whole number.",
                    {"quantity": "Whole numbers only for this item."},
                )
            available = free.get(body.variant_id, ZERO)
            if body.quantity > available:
                raise ApiError(
                    409,
                    "out_of_stock",
                    "Not enough stock for that quantity.",
                    {body.variant_id: qty_str(available)},
                )
        items = items_of(db, cart)
        current = next((i for i in items if i.variant_id == body.variant_id), None)
        if body.quantity == 0:
            if current is not None:
                db.delete(current)
                items.remove(current)
        else:
            if current is None and len(items) >= MAX_LINES:
                raise ApiError(409, "cart_full", f"A cart can hold up to {MAX_LINES} items.")
            if cart is None:
                customer = customer_of(ctx)
                if customer is None:
                    new_token = secrets.token_urlsafe(32)
                cart = Cart(
                    business_id=b,
                    customer_id=customer,
                    anonymous_token_hash=token_hash(new_token) if new_token else None,
                )
                db.add(cart)
                db.flush()
            if current is None:
                current = CartItem(
                    cart_id=cart.id,
                    business_id=b,
                    variant_id=body.variant_id,
                    quantity=body.quantity,
                )
                db.add(current)
                items.append(current)
            else:
                current.quantity = body.quantity
        if cart is not None:
            cart.updated_at = utc_now()
        db.commit()
        return cart_view(
            ctx, items_of(db, cart), **({"cart_token": new_token} if new_token else {})
        )

    @app.post("/internal/v1/cart/merge")
    def merge(body: CartRef, ctx: Ctx, db: Db):
        """After sign-in: move the anonymous cart into the customer's cart (quantities add up,
        capped at the per-line maximum). The anonymous cart is then removed."""
        b, customer = require_customer(ctx)
        if not body.cart_token or not TOKEN.fullmatch(body.cart_token):
            return cart_view(ctx, items_of(db, find_cart(db, ctx, None)))
        anon = db.scalar(
            select(Cart).where(
                Cart.business_id == b,
                Cart.status == "ACTIVE",
                Cart.customer_id.is_(None),
                Cart.anonymous_token_hash == token_hash(body.cart_token),
            )
        )
        mine = find_cart(db, ctx, None)
        if anon is not None:
            moving = items_of(db, anon)
            if mine is None:
                anon.customer_id, anon.anonymous_token_hash = customer, None  # adopt it
                mine = anon
            else:
                have = {i.variant_id: i for i in items_of(db, mine)}
                for item in moving:
                    if item.variant_id in have:
                        target = have[item.variant_id]
                        target.quantity = min(MAX_QTY, target.quantity + item.quantity)
                    elif len(have) < MAX_LINES:
                        have[item.variant_id] = CartItem(
                            cart_id=mine.id,
                            business_id=b,
                            variant_id=item.variant_id,
                            quantity=item.quantity,
                        )
                        db.add(have[item.variant_id])
                    db.delete(item)
                db.flush()
                db.delete(anon)
            db.commit()
        return cart_view(ctx, items_of(db, mine))

    # ---- pricing -------------------------------------------------------------------------
    def price(ctx: InternalContext, items: list[CartItem], ship_state: str | None) -> dict:
        """Everything checkout needs, computed from server-side facts only."""
        if not items:
            raise ApiError(409, "cart_empty", "Your cart is empty.")
        quoted, free = facts(ctx, [i.variant_id for i in items])
        problems = {}
        for item in items:
            info = quoted.get(item.variant_id)
            if info is None or not info["sellable"]:
                problems[item.variant_id] = "unavailable"
            elif item.quantity > free.get(item.variant_id, ZERO):
                problems[item.variant_id] = qty_str(free.get(item.variant_id, ZERO))
        if problems:
            raise ApiError(
                409,
                "cart_changed",
                "Some items in your cart are no longer available as chosen.",
                problems,
            )
        sctx = remote.service_ctx(ctx)
        profile = remote.profile(ctx)
        policy = remote.call("store", "GET", "/internal/v1/policies", sctx)
        goods = sum(
            (
                (Decimal(quoted[i.variant_id]["price"]) * i.quantity).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                for i in items
            ),
            ZERO,
        )
        free_over, flat = policy.get("free_shipping_over"), policy.get("flat_shipping_fee")
        if free_over is not None and goods >= Decimal(free_over):
            shipping = ZERO
        else:
            shipping = Decimal(flat) if flat is not None else ZERO
        seller_state = profile.get("state_code")
        inter = bool(ship_state and seller_state and ship_state != seller_state)
        tax = remote.call(
            "billing",
            "POST",
            "/internal/v1/tax/quote",
            sctx,
            json_body={
                "lines": [
                    {
                        "quantity": str(i.quantity),
                        "unit_price": quoted[i.variant_id]["price"],
                        "tax_class": quoted[i.variant_id]["tax_class"],
                    }
                    for i in items
                ],
                "seller_registered": profile["gst_registered"],
                "inter_state": inter,
                "include_tax": profile["prices_include_tax"],
                "shipping": money(shipping),
            },
        )
        return {"quoted": quoted, "tax": tax, "profile": profile, "inter_state": inter}

    def preview_out(items: list[CartItem], priced: dict) -> dict:
        tax, quoted = priced["tax"], priced["quoted"]
        return {
            "items": [
                {
                    "variant_id": i.variant_id,
                    "product_name": quoted[i.variant_id]["product_name"],
                    "sku": quoted[i.variant_id]["sku"],
                    "options": quoted[i.variant_id]["options"],
                    "quantity": qty_str(i.quantity),
                    "unit_price": quoted[i.variant_id]["price"],
                    "taxable_value": line["taxable"],
                    "tax": line["tax"],
                    "line_total": line["line_total"],
                }
                for i, line in zip(items, tax["lines"], strict=True)
            ],
            **{
                k: tax[k]
                for k in (
                    "subtotal",
                    "discount_total",
                    "tax_total",
                    "shipping_total",
                    "round_off",
                    "grand_total",
                )
            },
            "prices_include_tax": priced["profile"]["prices_include_tax"],
            "inter_state": priced["inter_state"],
            "payment_methods": ["COD"],
        }

    @app.post("/internal/v1/checkout/preview")
    def preview(body: PreviewIn, ctx: Ctx, db: Db):
        items = items_of(db, find_cart(db, ctx, None))
        if not customer_of(ctx):
            raise ApiError(401, "authentication_required", "Please sign in to continue.")
        return preview_out(items, price(ctx, items, body.ship_to_state_code))

    # ---- checkout ------------------------------------------------------------------------
    def replay(db: Session, b: str, customer: str, key: str) -> dict | None:
        o = db.scalar(select(Order).where(Order.business_id == b, Order.idempotency_key == key))
        if o is None:
            return None
        if o.customer_id != customer:
            raise ApiError(409, "idempotency_conflict", "This request key was already used.")
        items = list(db.scalars(select(OrderItem).where(OrderItem.order_id == o.id)))
        return order_out(o, items, replayed=True)

    def release(ctx: InternalContext, order_id: str) -> None:
        try:
            remote.call(
                "supply",
                "POST",
                f"/internal/v1/reservations/{order_id}/release",
                remote.service_ctx(ctx),
                json_body={"reason": "CANCELLED"},
            )
        except Exception:  # left ACTIVE: visible in reconciliation, never silently lost
            log.exception("could not release reservation for abandoned checkout %s", order_id)

    @app.post("/internal/v1/checkout", status_code=201)
    def checkout(body: CheckoutIn, ctx: Ctx, db: Db):
        b, customer = require_customer(ctx)
        key = scoped_key(customer, body.idempotency_key)
        done = replay(db, b, customer, key)
        if done is not None:
            return done
        cart = find_cart(db, ctx, None)
        items = items_of(db, cart)
        priced = price(ctx, items, body.ship_to.state_code)  # remote reads, no local writes yet
        order_id = new_id()
        remote.call(
            "supply",
            "POST",
            "/internal/v1/reservations",
            remote.service_ctx(ctx),
            json_body={
                "order_id": order_id,
                "idempotency_key": f"chk:{order_id}",
                "ttl_seconds": settings.cod_reservation_seconds,
                "lines": [{"variant_id": i.variant_id, "quantity": str(i.quantity)} for i in items],
            },
        )  # 409 out_of_stock passes straight back to the shopper
        tax, quoted = priced["tax"], priced["quoted"]
        prefix = priced["profile"]["invoice_prefix"]
        for attempt in range(3):
            try:
                now = utc_now()
                order = Order(
                    id=order_id,
                    business_id=b,
                    customer_id=customer,
                    order_number=order_number(prefix),
                    order_status="CONFIRMED",
                    payment_status="UNPAID",
                    fulfilment_status="UNFULFILLED",
                    payment_method="COD",
                    currency=priced["profile"]["currency"] or "INR",
                    subtotal=Decimal(tax["subtotal"]),
                    discount_total=Decimal(tax["discount_total"]),
                    tax_total=Decimal(tax["tax_total"]),
                    shipping_total=Decimal(tax["shipping_total"]),
                    round_off=Decimal(tax["round_off"]),
                    grand_total=Decimal(tax["grand_total"]),
                    ship_to=body.ship_to.model_dump(),
                    place_of_supply_state=body.ship_to.state_code,
                    idempotency_key=key,
                    placed_at=now,
                    created_at=now,
                    updated_at=now,
                )
                db.add(order)
                db.flush()
                rows = []
                for item, line in zip(items, tax["lines"], strict=True):
                    info = quoted[item.variant_id]
                    rows.append(
                        OrderItem(
                            business_id=b,
                            order_id=order_id,
                            product_id=info["product_id"],
                            variant_id=item.variant_id,
                            product_name=info["product_name"],
                            sku=info["sku"],
                            options=info["options"],
                            hsn_sac=info["hsn_sac"],
                            stock_unit=info["stock_unit"],
                            quantity=item.quantity,
                            unit_price=Decimal(info["price"]),
                            discount=Decimal(line["discount"]),
                            taxable_value=Decimal(line["taxable"]),
                            cgst=Decimal(line["cgst"]),
                            sgst=Decimal(line["sgst"]),
                            igst=Decimal(line["igst"]),
                            cess=Decimal(line["cess"]),
                            tax_class=info["tax_class"],
                            tax_rate_version_id=line["tax_rate_version_id"],
                            line_total=Decimal(line["line_total"]),
                        )
                    )
                db.add_all(rows)
                for axis, frm, to, actor in (
                    ("ORDER", None, "PENDING_PAYMENT", "CUSTOMER"),
                    ("PAYMENT", None, "UNPAID", "SYSTEM"),
                    ("ORDER", "PENDING_PAYMENT", "CONFIRMED", "SYSTEM"),
                ):
                    db.add(
                        OrderStatusHistory(
                            business_id=b,
                            order_id=order_id,
                            axis=axis,
                            from_status=frm,
                            to_status=to,
                            actor_type=actor,
                            actor_id=customer if actor == "CUSTOMER" else None,
                            note="Cash on delivery" if to == "CONFIRMED" else None,
                            created_at=now,
                        )
                    )
                cart.status = "CONVERTED"
                db.commit()
                return order_out(order, rows, replayed=False)
            except IntegrityError:
                db.rollback()
                done = replay(db, b, customer, key)
                if done is not None:  # a parallel request with the same key won the race
                    release(ctx, order_id)
                    return done
                if attempt == 2:  # three order-number collisions in a row: give up cleanly
                    release(ctx, order_id)
                    raise ApiError(409, "conflict", "Please try again.") from None
                cart = find_cart(db, ctx, None)
            except Exception:
                db.rollback()
                release(ctx, order_id)
                raise
        raise AssertionError("unreachable")
