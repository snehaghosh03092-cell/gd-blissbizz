"""Steps 6-7: cart, cash-on-delivery checkout with stock reservations, and the order lifecycle.

Stock facts used below (acme): TEE-S has MAIN on_hand 10 / reserved 2 and STORE on_hand 4, so 12
can be sold (8 from the default MAIN warehouse first). TEE-M has 2. Tee is GST_5, prices include
tax, the shop is in state 29, shipping is free from Rs 999 (else Rs 49, taxed at 18%).
"""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import sales_service.shop as shop_module
from app.db.models.sales import Order, OrderItem, OrderStatusHistory, Payment
from app.db.models.supply import InventoryBalance, InventoryTransaction, StockReservation
from blissbiz_common.context import InternalContext
from conftest import ORIGIN, PASSWORD, internal_call, login, shop, shopper_csrf, shopper_login
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

SF = "/api/v1/storefront"


@pytest.fixture
def cara(world):
    client = shop(world["gateway"])
    client.csrf = shopper_login(client)
    return client


def add(client, variant_id, quantity, csrf=None):
    return client.post(
        f"{SF}/cart/items",
        json={"variant_id": variant_id, "quantity": quantity},
        headers={"X-CSRF-Token": csrf or client.csrf},
    )


def checkout(client, address_id, key="checkout-key-0001", csrf=None):
    return client.post(
        f"{SF}/checkout",
        json={"address_id": address_id},
        headers={"X-CSRF-Token": csrf or client.csrf, "Idempotency-Key": key},
    )


def free(world, variant_id) -> Decimal:
    with Session(world["engine"]) as s:
        rows = s.execute(
            select(InventoryBalance.on_hand, InventoryBalance.reserved).where(
                InventoryBalance.variant_id == variant_id
            )
        ).all()
        return sum((a - b for a, b in rows), Decimal(0))


def balance(world, variant_id, code) -> tuple[Decimal, Decimal]:
    from app.db.models.supply import Warehouse

    with Session(world["engine"]) as s:
        b = s.scalar(
            select(InventoryBalance)
            .join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id)
            .where(InventoryBalance.variant_id == variant_id, Warehouse.code == code)
        )
        return b.on_hand, b.reserved


def count(world, model, *where) -> int:
    with Session(world["engine"]) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


# ---- cart -------------------------------------------------------------------------------
def test_anonymous_cart_then_merge_on_login(world):
    c = shop(world["gateway"])
    csrf = shopper_csrf(c)
    r = add(c, world["ids"]["tee_m"], 1, csrf)
    assert r.status_code == 200, r.text
    assert "cart_token" not in r.json() and c.cookies.get("blissbiz_cart")  # cookie, never JSON
    line = r.json()["items"][0]
    assert (line["unit_price"], line["line_total"], line["issue"]) == ("499.00", "499.00", None)
    shopper_login(c)
    assert c.cookies.get("blissbiz_cart") is None  # merged and cleared
    again = shop(world["gateway"])  # cara on another device sees the same cart
    shopper_login(again)
    assert [(i["sku"], i["quantity"]) for i in again.get(f"{SF}/cart").json()["items"]] == [
        ("TEE-M", "1")
    ]


def test_cart_rules(world, cara):
    ids = world["ids"]
    assert add(cara, ids["tee_s"], 1.5).status_code == 422  # pieces are whole numbers
    assert add(cara, ids["tee_s"], 100).status_code == 422  # over the per-line maximum
    hat = world["ids"]["hat"]
    with Session(world["engine"]) as s:
        from app.db.models.catalog import ProductVariant

        hat_variant = s.scalar(select(ProductVariant.id).where(ProductVariant.product_id == hat))
    assert add(cara, hat_variant, 1).status_code == 404  # draft product
    assert add(cara, ids["beta_variant"], 1).status_code == 404  # another shop's product
    short = add(cara, ids["tee_s"], 13)
    assert short.status_code == 409 and short.json()["error"]["fields"] == {ids["tee_s"]: "12"}
    tampered = cara.post(
        f"{SF}/cart/items",
        headers={"X-CSRF-Token": cara.csrf},
        json={"variant_id": ids["tee_s"], "quantity": 1, "price": "1.00"},
    )
    assert tampered.status_code == 422  # prices are never accepted from the browser
    assert add(cara, ids["tee_s"], 2).json()["subtotal"] == "998.00"
    assert add(cara, ids["tee_s"], 0).json()["items"] == []  # 0 removes
    no_csrf = cara.post(f"{SF}/cart/items", json={"variant_id": ids["tee_s"], "quantity": 1})
    assert no_csrf.status_code == 403


# ---- checkout ---------------------------------------------------------------------------
def test_checkout_intra_state_with_shipping_and_gst(world, cara):
    ids = world["ids"]
    add(cara, ids["tee_s"], 2)
    preview = cara.post(
        f"{SF}/checkout/preview",
        json={"address_id": ids["home"]},
        headers={"X-CSRF-Token": cara.csrf},
    ).json()
    assert Decimal(preview["grand_total"]) == Decimal("1047") and preview["payment_methods"] == [
        "COD"
    ]
    r = checkout(cara, ids["home"])
    assert r.status_code == 201, r.text
    o = r.json()
    # 2 x 499 = 998 incl. 5% GST: taxable 998/1.05 = 950.48, GST 47.52 (CGST 23.76 + SGST 23.76)
    # 998 < 999 so shipping Rs 49 incl. 18%: taxable 41.53, GST 7.47.  Total 998 + 49 = 1047.
    assert (
        o["subtotal"],
        o["shipping_total"],
        o["tax_total"],
        o["grand_total"],
        o["round_off"],
    ) == ("998.00", "49.00", "54.99", "1047.00", "0.00")
    line = o["items"][0]
    assert (line["taxable_value"], line["cgst"], line["sgst"], line["igst"]) == (
        "950.48",
        "23.76",
        "23.76",
        "0.00",
    )
    assert (o["order_status"], o["payment_status"], o["payment_method"]) == (
        "CONFIRMED",
        "UNPAID",
        "COD",
    )
    assert o["order_number"].startswith("AC-") and o["ship_to"]["city"] == "Bengaluru"
    assert free(world, ids["tee_s"]) == 10  # 2 held for this order
    assert balance(world, ids["tee_s"], "MAIN") == (10, 4)  # held in the default warehouse
    assert cara.get(f"{SF}/cart").json()["items"] == []  # the cart became the order


def test_checkout_inter_state_uses_igst_and_free_shipping(world, cara):
    ids = world["ids"]
    add(cara, ids["tee_s"], 3)
    o = checkout(cara, ids["away"]).json()  # Pune, state 27: inter-state
    # 1497 >= 999: free shipping. taxable 1497/1.05 = 1425.71, IGST 71.29
    assert (o["shipping_total"], o["tax_total"], o["grand_total"]) == ("0.00", "71.29", "1497.00")
    line = o["items"][0]
    assert (line["igst"], line["cgst"], o["place_of_supply_state"]) == ("71.29", "0.00", "27")


def test_same_idempotency_key_returns_the_same_order(world, cara):
    ids = world["ids"]
    add(cara, ids["tee_s"], 1)
    first = checkout(cara, ids["home"])
    again = checkout(cara, ids["home"])
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"] and again.json()["replayed"] is True
    assert count(world, Order) == 1 and count(world, StockReservation) == 1
    assert free(world, ids["tee_s"]) == 11


def test_checkout_requirements(world, cara):
    ids = world["ids"]
    anon = shop(world["gateway"])
    csrf = shopper_csrf(anon)
    assert checkout(anon, ids["home"], csrf=csrf).status_code == 401
    add(cara, ids["tee_s"], 1)
    no_key = cara.post(
        f"{SF}/checkout", json={"address_id": ids["home"]}, headers={"X-CSRF-Token": cara.csrf}
    )
    assert (
        no_key.status_code == 400 and no_key.json()["error"]["code"] == "idempotency_key_required"
    )
    no_csrf = cara.post(
        f"{SF}/checkout",
        json={"address_id": ids["home"]},
        headers={"Idempotency-Key": "checkout-key-0001"},
    )
    assert no_csrf.status_code == 403
    dan = shop(world["gateway"])
    dan_csrf = shopper_csrf(dan)
    dan.post(
        f"{SF}/auth/register",
        headers={"X-CSRF-Token": dan_csrf},
        json={"email": "dan@example.com", "password": PASSWORD, "full_name": "Dan"},
    )
    dan_csrf = shopper_csrf(dan)
    add(dan, ids["tee_s"], 1, dan_csrf)
    assert checkout(dan, ids["home"], csrf=dan_csrf).status_code == 404  # cara's address
    assert checkout(cara, ids["home"], key="checkout-key-0002").status_code == 201
    emptied = checkout(cara, ids["home"], key="checkout-key-0003")  # a new key, but no cart left
    assert emptied.status_code == 409 and emptied.json()["error"]["code"] == "cart_empty"


def test_stock_split_across_warehouses(world, cara):
    ids = world["ids"]
    add(cara, ids["tee_s"], 11)
    assert checkout(cara, ids["home"]).status_code == 201
    with Session(world["engine"]) as s:
        held = sorted(s.scalars(select(StockReservation.quantity)))
    assert held == [3, 8]  # 8 from MAIN (default) + 3 from STORE
    assert free(world, ids["tee_s"]) == 1


def test_last_units_cannot_be_sold_twice(world, cara):
    """Cara puts the last 2 TEE-M in her cart, Dan checks out first: Cara is told, not oversold."""
    ids = world["ids"]
    add(cara, ids["tee_m"], 2)
    dan = shop(world["gateway"])
    csrf = shopper_csrf(dan)
    dan.post(
        f"{SF}/auth/register",
        headers={"X-CSRF-Token": csrf},
        json={"email": "dan@example.com", "password": PASSWORD, "full_name": "Dan"},
    )
    csrf = shopper_csrf(dan)
    made = dan.post(
        f"{SF}/me/addresses",
        headers={"X-CSRF-Token": csrf},
        json={
            "recipient": "Dan",
            "phone": "9822222222",
            "line1": "3 Lake Rd",
            "city": "Mysuru",
            "state_code": "29",
            "postal_code": "570001",
        },
    ).json()
    add(dan, ids["tee_m"], 2, csrf)
    assert checkout(dan, made["id"], csrf=csrf).status_code == 201
    view = cara.get(f"{SF}/cart").json()
    assert view["has_issues"] and view["items"][0]["issue"] == "insufficient_stock"
    r = checkout(cara, ids["home"])
    assert (
        r.status_code == 409 and r.json()["error"]["code"] == "cart_changed"
    )  # same key as Dan: fine
    assert r.json()["error"]["fields"] == {ids["tee_m"]: "0"}
    assert free(world, ids["tee_m"]) == 0 and count(world, Order) == 1


def test_supply_reservations_are_all_or_nothing_and_sales_only(world):
    ids, supply = world["ids"], world["supply"]
    sales_ctx = InternalContext("req-12345678", "SERVICE", ids["acme"], "sales")
    body = {
        "order_id": "order-1",
        "idempotency_key": "chk:order-1",
        "ttl_seconds": 600,
        "lines": [
            {"variant_id": ids["tee_s"], "quantity": "1"},
            {"variant_id": ids["tee_m"], "quantity": "5"},
            {"variant_id": ids["beta_variant"], "quantity": "1"},
        ],
    }
    status, data = internal_call(supply, "POST", "/internal/v1/reservations", sales_ctx, body)
    assert status == 409 and data["error"]["fields"] == {
        ids["tee_m"]: "2",
        ids["beta_variant"]: "0",
    }
    assert free(world, ids["tee_s"]) == 12  # nothing was held
    gateway_ctx = InternalContext("req-12345678", "SERVICE", ids["acme"], "gateway")
    assert internal_call(supply, "POST", "/internal/v1/reservations", gateway_ctx, body)[0] == 403
    body["lines"] = body["lines"][:1]
    first = internal_call(supply, "POST", "/internal/v1/reservations", sales_ctx, body)
    second = internal_call(supply, "POST", "/internal/v1/reservations", sales_ctx, body)
    assert first[0] == 201 and second[1]["replayed"] is True
    assert second[1]["reservations"][0]["id"] == first[1]["reservations"][0]["id"]
    path = "/internal/v1/reservations/order-1/release"
    assert (
        internal_call(supply, "POST", path, sales_ctx, {"reason": "CANCELLED"})[1]["released_now"]
        == 1
    )
    assert (
        internal_call(supply, "POST", path, sales_ctx, {"reason": "CANCELLED"})[1]["released_now"]
        == 0
    )
    assert free(world, ids["tee_s"]) == 12


def test_failure_after_reserving_releases_the_stock(world, cara, monkeypatch):
    """Compensation: the order could not be written, so the held stock is given back."""
    ids = world["ids"]
    add(cara, ids["tee_s"], 2)

    def broken(prefix):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(shop_module, "order_number", broken)
    r = checkout(cara, ids["home"])
    assert r.status_code == 500 and "hiccup" not in r.text  # no internals leak
    assert count(world, Order) == 0 and free(world, ids["tee_s"]) == 12
    assert count(world, StockReservation, StockReservation.status == "ACTIVE") == 0
    assert count(world, StockReservation, StockReservation.status == "RELEASED") == 1


def test_supply_down_means_no_order(world, cara):
    ids = world["ids"]
    add(cara, ids["tee_s"], 1)

    def down(request):
        raise httpx.ConnectError("supply is down")

    sales = world["make_sales"](supply=httpx.MockTransport(down))
    gateway = world["make_gateway"](sales_transport=httpx.ASGITransport(app=sales))
    broken = shop(gateway)
    broken.cookies, broken.csrf = cara.cookies, cara.csrf
    r = checkout(broken, ids["home"])
    assert r.status_code == 503 and r.json()["error"]["code"] == "service_unavailable"
    assert count(world, Order) == 0


# ---- order lifecycle (step 7) ---------------------------------------------------------------
@pytest.fixture
def placed(world, cara):
    add(cara, world["ids"]["tee_s"], 2)
    order = checkout(cara, world["ids"]["home"]).json()
    r = login(world["client"])  # alice owns acme
    world["client"].csrf = r.json()["csrf_token"]
    return order


def act(world, order_id, action, body=None, client=None, business="acme"):
    client = client or world["client"]
    url = f"/api/v1/admin/businesses/{world['ids'][business]}/orders/{order_id}/{action}"
    return client.post(
        url, json=body or {}, headers={"X-CSRF-Token": client.csrf, "Origin": ORIGIN}
    )


def test_owner_sees_orders(world, placed):
    base = f"/api/v1/admin/businesses/{world['ids']['acme']}/orders"
    listing = world["client"].get(base).json()
    assert listing["total"] == 1 and listing["items"][0]["order_number"] == placed["order_number"]
    assert world["client"].get(f"{base}?status=shipped").json()["total"] == 0
    assert world["client"].get(f"{base}?q={placed['order_number'][:5]}").json()["total"] == 1
    detail = world["client"].get(f"{base}/{placed['id']}").json()
    assert [h["to"] for h in detail["history"]] == ["PENDING_PAYMENT", "UNPAID", "CONFIRMED"]
    other = f"/api/v1/admin/businesses/{world['ids']['beta']}/orders/{placed['id']}"
    assert world["client"].get(other).status_code == 404  # alice manages beta, but not this order


def test_pack_ship_deliver_moves_stock_and_collects_cash(world, placed):
    ids, oid = world["ids"], placed["id"]
    assert act(world, oid, "pack").json()["order_status"] == "PACKED"
    shipped = act(world, oid, "ship", {"carrier": "Delhivery", "tracking_number": "DL123"})
    assert shipped.status_code == 200, shipped.text
    assert shipped.json()["fulfilment_status"] == "SHIPPED"
    assert shipped.json()["shipments"][0]["tracking_number"] == "DL123"
    assert balance(world, ids["tee_s"], "MAIN") == (8, 2)  # left the shelf: on_hand -2, hold -2
    with Session(world["engine"]) as s:
        sale = s.scalars(
            select(InventoryTransaction).where(InventoryTransaction.txn_type == "SALE")
        ).all()
    assert [(t.quantity_delta, t.source_id, t.unit_cost) for t in sale] == [
        (-2, oid, Decimal("250"))
    ]
    assert act(world, oid, "ship").status_code == 200  # retry: nothing happens twice
    assert count(world, InventoryTransaction, InventoryTransaction.txn_type == "SALE") == 1
    done = act(world, oid, "deliver").json()
    assert (done["order_status"], done["payment_status"]) == ("DELIVERED", "PAID")
    assert done["payments"] == [
        {"provider": "cod", "method": "cash", "status": "CAPTURED", "amount": "1047.00"}
    ]
    act(world, oid, "deliver")
    assert count(world, Payment) == 1
    assert act(world, oid, "cancel").json()["error"]["code"] == "invalid_transition"


def test_paid_order_without_a_payment_record_is_refused_and_keeps_its_stock(world, placed):
    with Session(world["engine"]) as s:  # inconsistent data: marked paid, no payment row
        s.execute(update(Order).values(payment_status="PAID"))
        s.commit()
    r = act(world, placed["id"], "cancel")
    assert r.status_code == 409 and r.json()["error"]["code"] == "nothing_to_refund"
    assert free(world, world["ids"]["tee_s"]) == 10  # checked before any stock was touched


def test_illegal_transitions(world, placed):
    oid = placed["id"]
    assert act(world, oid, "deliver").json()["error"]["code"] == "invalid_transition"
    act(world, oid, "cancel", {"reason": "Customer called"})
    for action in ("pack", "ship", "deliver"):
        assert act(world, oid, action).status_code == 409


def test_owner_cancel_releases_stock(world, placed):
    ids = world["ids"]
    assert free(world, ids["tee_s"]) == 10
    r = act(world, placed["id"], "cancel", {"reason": "Customer called"})
    assert (
        r.json()["order_status"] == "CANCELLED" and r.json()["cancel_reason"] == "Customer called"
    )
    assert free(world, ids["tee_s"]) == 12 and balance(world, ids["tee_s"], "MAIN") == (10, 2)
    assert count(world, InventoryTransaction, InventoryTransaction.txn_type == "SALE") == 0


def test_ship_refused_when_the_order_holds_no_stock(world, placed):
    ctx = InternalContext("req-12345678", "SERVICE", world["ids"]["acme"], "sales")
    internal_call(
        world["supply"],
        "POST",
        f"/internal/v1/reservations/{placed['id']}/release",
        ctx,
        {"reason": "EXPIRED"},
    )
    r = act(world, placed["id"], "ship")
    assert r.status_code == 409 and r.json()["error"]["code"] == "stock_not_reserved"


def test_staff_can_fulfil_but_customers_and_strangers_cannot(world, placed, cara):
    from fastapi.testclient import TestClient

    bob = TestClient(
        world["gateway"], base_url="http://127.0.0.1:5173", raise_server_exceptions=False
    )
    bob.csrf = login(bob, email="bob@example.com").json()["csrf_token"]  # STAFF of acme
    assert act(world, placed["id"], "pack", client=bob).status_code == 200
    dave = TestClient(
        world["gateway"], base_url="http://127.0.0.1:5173", raise_server_exceptions=False
    )
    dave.csrf = login(dave, email="dave@example.com").json()["csrf_token"]
    assert act(world, placed["id"], "ship", client=dave).status_code == 404  # not a member
    url = f"/api/v1/admin/businesses/{world['ids']['acme']}/orders"
    admin_host = TestClient(
        world["gateway"], base_url="http://127.0.0.1:5173", raise_server_exceptions=False
    )
    admin_host.cookies.set("blissbiz_session", cara.cookies.get("blissbiz_session"))
    assert admin_host.get(url).status_code == 401  # a customer session is not an owner session


def test_customer_orders_and_cancellation(world, placed, cara):
    mine = cara.get(f"{SF}/orders").json()
    assert mine["total"] == 1 and mine["items"][0]["id"] == placed["id"]
    assert "payments" not in cara.get(f"{SF}/orders/{placed['id']}").json()
    beta = shop(world["gateway"], "beta.localhost")
    shopper_login(beta)
    assert beta.get(f"{SF}/orders/{placed['id']}").status_code == 404  # other shop's account
    with Session(world["engine"]) as s:  # pretend the order is 2 days old (window is 24 hours)
        order = s.get(Order, placed["id"])
        order.placed_at -= timedelta(days=2)
        s.commit()
    late = cara.post(
        f"{SF}/orders/{placed['id']}/cancel", json={}, headers={"X-CSRF-Token": cara.csrf}
    )
    assert late.json()["error"]["code"] == "cancel_window_passed"
    with Session(world["engine"]) as s:
        order = s.get(Order, placed["id"])
        order.placed_at += timedelta(days=2)
        s.commit()
    ok = cara.post(
        f"{SF}/orders/{placed['id']}/cancel", json={}, headers={"X-CSRF-Token": cara.csrf}
    )
    assert ok.json()["order_status"] == "CANCELLED" and free(world, world["ids"]["tee_s"]) == 12
    with Session(world["engine"]) as s:
        actors = s.scalars(
            select(OrderStatusHistory.actor_type).where(OrderStatusHistory.to_status == "CANCELLED")
        ).all()
    assert actors == ["CUSTOMER"]


def test_customer_cannot_cancel_once_packed(world, placed, cara):
    act(world, placed["id"], "pack")
    r = cara.post(
        f"{SF}/orders/{placed['id']}/cancel", json={}, headers={"X-CSRF-Token": cara.csrf}
    )
    assert r.status_code == 409


def test_order_lines_are_snapshots(world, placed):
    """Renaming the product later does not rewrite what the customer bought."""
    from app.db.models.catalog import Product

    with Session(world["engine"]) as s:
        s.execute(update(Product).where(Product.id == world["ids"]["tee"]).values(name="Renamed"))
        s.commit()
        names = s.scalars(select(OrderItem.product_name)).all()
    assert names == ["Tee"]
