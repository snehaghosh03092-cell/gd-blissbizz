"""GST invoices at dispatch, credit notes, returns, refunds, paid-order cancellation, expiry.

Acme is GST-registered (state 29, prefix AC, prices include tax); Beta is not registered.
Cara's 2 x Tee (Rs 499, 5% GST) to Bengaluru: goods 998 (taxable 950.48, CGST 23.76, SGST 23.76)
plus shipping 49 (taxable 41.53, GST 7.47) = 1047.
"""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from app.db.models import utc_now
from app.db.models.billing import CreditNote, Invoice, InvoiceLine, InvoiceSequence
from app.db.models.sales import Order, Payment, Refund
from app.db.models.supply import InventoryTransaction, StockReservation
from blissbiz_common.context import InternalContext
from conftest import ORIGIN, PASSWORD, internal_call, login, shop, shopper_csrf, shopper_login
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

SF = "/api/v1/storefront"


def count(world, model, *where) -> int:
    with Session(world["engine"]) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def free(world, variant_id) -> Decimal:
    from app.db.models.supply import InventoryBalance

    with Session(world["engine"]) as s:
        rows = s.execute(
            select(InventoryBalance.on_hand, InventoryBalance.reserved).where(
                InventoryBalance.variant_id == variant_id
            )
        ).all()
        return sum((a - b for a, b in rows), Decimal(0))


@pytest.fixture
def cara(world):
    c = shop(world["gateway"])
    c.csrf = shopper_login(c)
    return c


@pytest.fixture
def owner(world):
    c = world["client"]
    c.csrf = login(c).json()["csrf_token"]  # alice: OWNER of acme, MANAGER of beta
    return c


def place(world, client, qty=2, key="key-000000001", address=None):
    h = {"X-CSRF-Token": client.csrf}
    assert (
        client.post(
            f"{SF}/cart/items",
            json={"variant_id": world["ids"]["tee_s"], "quantity": qty},
            headers=h,
        ).status_code
        == 200
    )
    r = client.post(
        f"{SF}/checkout",
        json={"address_id": address or world["ids"]["home"]},
        headers={**h, "Idempotency-Key": key},
    )
    assert r.status_code == 201, r.text
    return r.json()


def act(world, owner, order_id, action, body=None, business="acme"):
    url = f"/api/v1/admin/businesses/{world['ids'][business]}/orders/{order_id}/{action}"
    return owner.post(url, json=body or {}, headers={"X-CSRF-Token": owner.csrf, "Origin": ORIGIN})


def admin(world, owner, method, tail, body=None, business="acme"):
    url = f"/api/v1/admin/businesses/{world['ids'][business]}/{tail}"
    headers = {"Origin": ORIGIN} | ({"X-CSRF-Token": owner.csrf} if method != "GET" else {})
    return owner.request(method, url, json=body if method != "GET" else None, headers=headers)


def deliver(world, owner, order_id):
    for action in ("ship", "deliver"):
        assert act(world, owner, order_id, action).status_code == 200


# ---- invoices -------------------------------------------------------------------------------
def test_shipping_issues_a_gapless_gst_invoice(world, cara, owner):
    o = place(world, cara)
    shipped = act(world, owner, o["id"], "ship").json()
    assert (
        shipped["invoice"]["number"] == "AC/26-27/00001"
        and shipped["invoice"]["doc_type"] == "TAX_INVOICE"
    )
    inv = admin(world, owner, "GET", f"orders/{o['id']}/invoice").json()
    goods, shipping = inv["lines"]
    assert (goods["taxable_value"], goods["cgst"], goods["sgst"], goods["cgst_rate"]) == (
        "950.48",
        "23.76",
        "23.76",
        "2.5",
    )
    assert (shipping["hsn_sac"], shipping["taxable_value"], shipping["cgst"], shipping["sgst"]) == (
        "996812",
        "41.53",
        "3.74",
        "3.73",
    )
    assert (inv["grand_total"], inv["taxable_total"], inv["round_off"]) == (
        "1047.00",
        "992.01",
        "0.00",
    )
    assert inv["seller"]["gstin"] == "29ABCDE1234F1Z5" and inv["buyer"]["state_code"] == "29"
    second = place(world, cara, 1, key="key-000000002")
    act(world, owner, second["id"], "ship")
    with Session(world["engine"]) as s:
        numbers = s.scalars(select(Invoice.display_number).order_by(Invoice.number)).all()
        seq = s.scalar(select(InvoiceSequence.last_number).where(InvoiceSequence.series == "AC"))
        line_sum = s.scalar(select(func.sum(InvoiceLine.line_total)))
    assert numbers == ["AC/26-27/00001", "AC/26-27/00002"] and seq == 2
    assert line_sum == Decimal("1047.00") + Decimal("499.00") + Decimal("49.00")
    act(world, owner, o["id"], "ship")  # retry: still one invoice per order
    assert count(world, Invoice) == 2


def test_invoice_visibility(world, cara, owner):
    o = place(world, cara)
    act(world, owner, o["id"], "ship")
    assert cara.get(f"{SF}/orders/{o['id']}/invoice").json()["number"] == "AC/26-27/00001"
    dan = shop(world["gateway"])
    csrf = shopper_csrf(dan)
    dan.post(
        f"{SF}/auth/register",
        headers={"X-CSRF-Token": csrf},
        json={"email": "dan@example.com", "password": PASSWORD, "full_name": "Dan"},
    )
    assert dan.get(f"{SF}/orders/{o['id']}/invoice").status_code == 404  # not dan's order
    listing = admin(world, owner, "GET", "invoices").json()
    assert listing["total"] == 1 and listing["items"][0]["number"] == "AC/26-27/00001"
    assert admin(world, owner, "GET", "invoices", business="beta").json()["total"] == 0


def test_invoice_before_shipping_is_not_found(world, cara, owner):
    o = place(world, cara)
    r = admin(world, owner, "GET", f"orders/{o['id']}/invoice")
    assert r.status_code == 404 and r.json()["error"]["code"] == "invoice_not_found"


def test_billing_down_at_dispatch_still_ships_and_invoices_later(world, cara, owner):
    o = place(world, cara)

    def down(request):
        raise httpx.ConnectError("billing is down")

    broken_sales = world["make_sales"](billing=httpx.MockTransport(down))
    gw = world["make_gateway"](sales_transport=httpx.ASGITransport(app=broken_sales))
    c = TestClient(gw, base_url="http://127.0.0.1:5173", raise_server_exceptions=False)
    c.cookies, c.csrf = owner.cookies, owner.csrf
    r = act(world, c, o["id"], "ship")
    assert r.status_code == 200 and r.json()["order_status"] == "SHIPPED"
    assert r.json()["invoice"] == {"pending": True, "unavailable": True}
    assert count(world, Invoice) == 0
    inv = admin(world, owner, "GET", f"orders/{o['id']}/invoice")  # billing is back: issued now
    assert inv.status_code == 200 and inv.json()["number"] == "AC/26-27/00001"


def test_unregistered_seller_issues_a_bill_of_supply_without_tax(world, owner):
    ids = world["ids"]
    bea = shop(world["gateway"], "beta.localhost")
    bea.csrf = shopper_login(bea)
    address = bea.post(
        f"{SF}/me/addresses",
        headers={"X-CSRF-Token": bea.csrf},
        json={
            "recipient": "Bea",
            "phone": "9811112222",
            "line1": "4 Hill Rd",
            "city": "Mumbai",
            "state_code": "27",
            "postal_code": "400050",
        },
    ).json()
    bea.post(
        f"{SF}/cart/items",
        json={"variant_id": ids["beta_variant"], "quantity": 1},
        headers={"X-CSRF-Token": bea.csrf},
    )
    o = bea.post(
        f"{SF}/checkout",
        json={"address_id": address["id"]},
        headers={"X-CSRF-Token": bea.csrf, "Idempotency-Key": "beta-key-0001"},
    ).json()
    assert o["tax_total"] == "0.00"
    shipped = act(world, owner, o["id"], "ship", business="beta").json()  # alice manages beta
    assert shipped["invoice"]["doc_type"] == "BILL_OF_SUPPLY" and shipped["invoice"][
        "number"
    ].startswith("BET/")
    inv = admin(world, owner, "GET", f"orders/{o['id']}/invoice", business="beta").json()
    assert inv["cgst_total"] == inv["sgst_total"] == inv["igst_total"] == "0.00"


# ---- returns, credit notes and refunds --------------------------------------------------------
def request_return(cara, order, qty, reason="Too small"):
    item = order["items"][0]["id"]
    return cara.post(
        f"{SF}/orders/{order['id']}/returns",
        headers={"X-CSRF-Token": cara.csrf},
        json={"reason": reason, "items": [{"order_item_id": item, "quantity": qty}]},
    )


def run_return(world, owner, rid, damaged=None, method="UPI"):
    for action, body in (
        ("approve", {}),
        ("receive", {"damaged_item_ids": damaged or []}),
        ("refund", {"method": method, "reference": "UPI-778899"}),
    ):
        r = admin(world, owner, "POST", f"returns/{rid}/{action}", body)
        assert r.status_code == 200, (action, r.text)
    return r.json()


def test_return_in_two_parts_credits_exactly_the_invoiced_goods(world, cara, owner):
    ids = world["ids"]
    o = place(world, cara)
    deliver(world, owner, o["id"])
    before = free(world, ids["tee_s"])
    first = request_return(cara, o, 1)
    assert first.status_code == 201 and first.json()["order_status"] == "RETURN_REQUESTED"
    assert request_return(cara, o, 1).json()["error"]["code"] == "return_open"  # one at a time
    done = run_return(world, owner, first.json()["returns"][0]["id"])
    assert done["order_status"] == "RETURNED" and done["payment_status"] == "PARTIALLY_REFUNDED"
    assert free(world, ids["tee_s"]) == before + 1  # back on the shelf
    # half of the line: taxable 950.48/2 = 475.24, CGST 11.88, SGST 11.88 -> 499.00
    notes = done["invoice"]["credit_notes"]
    assert [
        (n["number"], n["taxable_total"], n["cgst_total"], n["grand_total"]) for n in notes
    ] == [("CN/26-27/00001", "475.24", "11.88", "499.00")]
    assert request_return(cara, o, 2).json()["error"]["fields"] == {
        o["items"][0]["id"]: "At most 1 can be returned."
    }
    second = request_return(cara, o, 1).json()["returns"][1]["id"]
    done = run_return(world, owner, second)
    notes = done["invoice"]["credit_notes"]
    assert sum(Decimal(n["taxable_total"]) for n in notes) == Decimal(
        "950.48"
    )  # never a paisa more
    assert [r["amount"] for r in done["refunds"]] == ["499.00", "499.00"]
    assert done["payment_status"] == "PARTIALLY_REFUNDED"  # shipping (49) is not refunded
    assert request_return(cara, o, 1).json()["error"]["code"] == "too_many"
    with Session(world["engine"]) as s:
        assert s.scalar(select(func.sum(Refund.amount))) == Decimal("998.00")
        assert s.scalar(select(func.sum(CreditNote.grand_total))) <= Decimal("1047.00")


def test_damaged_returns_are_written_off_and_refunds_are_idempotent(world, cara, owner):
    ids = world["ids"]
    o = place(world, cara)
    deliver(world, owner, o["id"])
    before = free(world, ids["tee_s"])
    rid = request_return(cara, o, 2).json()["returns"][0]["id"]
    item = admin(world, owner, "GET", f"orders/{o['id']}").json()["returns"][0]["items"][0]["id"]
    done = run_return(world, owner, rid, damaged=[item])
    assert free(world, ids["tee_s"]) == before  # came back and was written off as damaged
    with Session(world["engine"]) as s:
        kinds = s.scalars(
            select(InventoryTransaction.txn_type)
            .where(InventoryTransaction.source_id == rid)
            .order_by(InventoryTransaction.id)
        ).all()
    assert kinds == ["CUSTOMER_RETURN", "DAMAGE"]
    assert (
        admin(world, owner, "POST", f"returns/{rid}/refund", {"method": "CASH"}).status_code == 200
    )
    assert count(world, Refund) == 1 and count(world, CreditNote) == 1  # retry changed nothing
    assert done["refunds"][0]["amount"] == "998.00"


def test_return_rules(world, cara, owner):
    o = place(world, cara)
    assert request_return(cara, o, 1).json()["error"]["code"] == "not_returnable"  # not delivered
    deliver(world, owner, o["id"])
    assert request_return(cara, o, 0.5).json()["error"]["fields"] == {
        o["items"][0]["id"]: "Whole numbers only."
    }
    rid = request_return(cara, o, 1).json()["returns"][0]["id"]
    assert (
        admin(world, owner, "POST", f"returns/{rid}/receive").json()["error"]["code"]
        == "invalid_transition"
    )
    rejected = admin(world, owner, "POST", f"returns/{rid}/reject", {"note": "Worn"}).json()
    assert rejected["order_status"] == "DELIVERED"
    with Session(world["engine"]) as s:  # acme accepts returns for 7 days after delivery
        s.execute(update(Order).values(updated_at=utc_now() - timedelta(days=60)))
        from app.db.models.sales import Shipment

        s.execute(update(Shipment).values(delivered_at=utc_now() - timedelta(days=60)))
        s.commit()
    assert request_return(cara, o, 1).json()["error"]["code"] == "return_window_passed"


def test_staff_cannot_refund(world, cara, owner):
    o = place(world, cara)
    deliver(world, owner, o["id"])
    rid = request_return(cara, o, 1).json()["returns"][0]["id"]
    bob = TestClient(
        world["gateway"], base_url="http://127.0.0.1:5173", raise_server_exceptions=False
    )
    bob.csrf = login(bob, email="bob@example.com").json()["csrf_token"]
    assert admin(world, bob, "POST", f"returns/{rid}/approve").status_code == 403  # sales.returns
    admin(world, owner, "POST", f"returns/{rid}/approve")
    admin(world, owner, "POST", f"returns/{rid}/receive")
    assert admin(world, bob, "POST", f"returns/{rid}/refund", {"method": "CASH"}).status_code == 403


# ---- cancelling a paid order --------------------------------------------------------------------
def test_cancelling_a_paid_online_order_restocks_and_refunds(world, cara, owner):
    """Online orders commit stock at payment; cancelling one puts the stock back and refunds."""
    ids = world["ids"]
    o = place(world, cara)
    ctx = InternalContext("req-12345678", "SERVICE", ids["acme"], "sales")
    internal_call(world["supply"], "POST", f"/internal/v1/reservations/{o['id']}/commit", ctx, {})
    with Session(world["engine"]) as s:  # what a captured online payment looks like
        s.execute(update(Order).values(payment_method="ONLINE", payment_status="PAID"))
        s.add(
            Payment(
                business_id=ids["acme"],
                order_id=o["id"],
                provider="fake",
                provider_order_id="fo_1",
                provider_payment_id="fp_1",
                method="upi",
                status="CAPTURED",
                amount=Decimal("1047"),
            )
        )
        s.commit()
    assert free(world, ids["tee_s"]) == 10
    r = act(world, owner, o["id"], "cancel", {"reason": "Out of delivery area"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["order_status"], body["payment_status"]) == ("CANCELLED", "REFUNDED")
    assert body["refunds"][0]["amount"] == "1047.00" and free(world, ids["tee_s"]) == 12
    with Session(world["engine"]) as s:
        back = s.scalar(
            select(InventoryTransaction.reason_code).where(
                InventoryTransaction.txn_type == "CUSTOMER_RETURN"
            )
        )
    assert back == "ORDER_CANCELLED"
    assert act(world, owner, o["id"], "cancel").status_code == 200 and count(world, Refund) == 1


# ---- reservation expiry -------------------------------------------------------------------------
def sweep(world):
    ctx = InternalContext("req-12345678", "SERVICE", None, "scheduler")
    status, body = internal_call(
        world["sales"], "POST", "/internal/v1/jobs/expire-reservations", ctx
    )
    assert status == 200, body
    return body


def expire(world, order_id=None):
    with Session(world["engine"]) as s:
        q = update(StockReservation).values(expires_at=utc_now() - timedelta(minutes=1))
        if order_id:
            q = q.where(StockReservation.order_id == order_id)
        s.execute(q)
        s.commit()


def test_expired_unpacked_cod_orders_are_cancelled_packed_ones_are_kept(world, cara, owner):
    ids = world["ids"]
    waiting = place(world, cara, 1, key="key-000000001")
    packed = place(world, cara, 1, key="key-000000002")
    act(world, owner, packed["id"], "pack")
    assert sweep(world)["checked"] == 0  # nothing expired yet
    expire(world)
    result = sweep(world)
    assert (result["cancelled"], result["extended"]) == (1, 1)
    w = admin(world, owner, "GET", f"orders/{waiting['id']}").json()
    assert w["order_status"] == "CANCELLED" and "hold expired" in w["cancel_reason"]
    assert admin(world, owner, "GET", f"orders/{packed['id']}").json()["order_status"] == "PACKED"
    assert free(world, ids["tee_s"]) == 11  # one unit back, the packed one still held
    with Session(world["engine"]) as s:
        statuses = dict(s.execute(select(StockReservation.order_id, StockReservation.status)).all())
        packed_until = s.scalar(
            select(StockReservation.expires_at).where(StockReservation.order_id == packed["id"])
        )
    assert (
        statuses == {waiting["id"]: "EXPIRED", packed["id"]: "ACTIVE"} and packed_until > utc_now()
    )
    assert sweep(world)["checked"] == 0  # nothing left to do
    assert (
        act(world, owner, packed["id"], "ship").status_code == 200
    )  # the packed order still ships


def test_expiry_releases_orphans_and_fails_unpaid_online_orders(world, cara):
    ids = world["ids"]
    ctx = InternalContext("req-12345678", "SERVICE", ids["acme"], "sales")
    internal_call(
        world["supply"],
        "POST",
        "/internal/v1/reservations",
        ctx,
        {  # a crashed checkout
            "order_id": "orphan-1",
            "idempotency_key": "chk:orphan-1",
            "ttl_seconds": 600,
            "lines": [{"variant_id": ids["tee_s"], "quantity": "3"}],
        },
    )
    pending = place(world, cara, 1)
    with Session(world["engine"]) as s:
        s.execute(
            update(Order).values(
                payment_method="ONLINE", payment_status="PENDING", order_status="PENDING_PAYMENT"
            )
        )
        s.add(
            Payment(
                business_id=ids["acme"],
                order_id=pending["id"],
                provider="fake",
                provider_order_id="fo_2",
                method="upi",
                status="PENDING",
                amount=Decimal("548"),
            )
        )
        s.commit()
    expire(world)
    result = sweep(world)
    assert (result["released"], result["cancelled"]) == (1, 1)
    assert free(world, ids["tee_s"]) == 12
    with Session(world["engine"]) as s:
        o = s.get(Order, pending["id"])
        pay = s.scalar(select(Payment).where(Payment.order_id == pending["id"]))
    assert (o.order_status, o.payment_status, pay.status, pay.failure_code) == (
        "CANCELLED",
        "FAILED",
        "FAILED",
        "RESERVATION_EXPIRED",
    )


def test_only_services_can_trigger_the_sweep(world):
    ctx = InternalContext(
        "req-12345678", "OWNER", world["ids"]["acme"], "x", "OWNER", ("sales.fulfil",)
    )
    status, _ = internal_call(world["sales"], "POST", "/internal/v1/jobs/expire-reservations", ctx)
    assert status == 403
