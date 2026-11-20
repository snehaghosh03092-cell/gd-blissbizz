"""Step 11: purchasing. Suppliers, purchase orders, goods receipts (stock + average cost),
supplier documents, supplier invoices (review, corrections, approval) and supplier payments."""

import base64
from decimal import Decimal

import pytest
from app.db.models.supply import InventoryBalance, InventoryTransaction, Warehouse
from conftest import ORIGIN, login
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
def owner(world):
    c = world["client"]
    c.csrf = login(c).json()["csrf_token"]
    return c


@pytest.fixture
def bob(world):  # STAFF: may receive goods, may not approve
    c = TestClient(
        world["gateway"], base_url="http://127.0.0.1:5173", raise_server_exceptions=False
    )
    c.csrf = login(c, email="bob@example.com").json()["csrf_token"]
    return c


def admin(world, client, method, tail, body=None, business="acme"):
    url = f"/api/v1/admin/businesses/{world['ids'][business]}/{tail}"
    headers = {"Origin": ORIGIN} | ({"X-CSRF-Token": client.csrf} if method != "GET" else {})
    return client.request(
        method, url, json=body if method not in ("GET",) else None, headers=headers
    )


def main_warehouse(world) -> str:
    with Session(world["engine"]) as s:
        return s.scalar(
            select(Warehouse.id).where(
                Warehouse.business_id == world["ids"]["acme"], Warehouse.code == "MAIN"
            )
        )


def supplier(world, owner, name="Loom Textiles"):
    r = admin(
        world,
        owner,
        "POST",
        "suppliers",
        {"name": name, "payment_terms_days": 30, "lead_time_days": 5},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_suppliers(world, owner):
    s = supplier(world, owner)
    assert admin(world, owner, "POST", "suppliers", {"name": "Loom Textiles"}).status_code == 409
    bad = admin(world, owner, "POST", "suppliers", {"name": "X", "gstin": "29ABCDE1234F1Z9"})
    assert bad.status_code == 422  # wrong checksum
    good = admin(world, owner, "POST", "suppliers", {"name": "Y", "gstin": "29abcde1234f1zw"})
    assert good.json()["gstin"] == "29ABCDE1234F1ZW"
    admin(world, owner, "PATCH", f"suppliers/{s['id']}", {"archived": True})
    assert [x["name"] for x in admin(world, owner, "GET", "suppliers").json()["items"]] == ["Y"]


def test_purchase_order_to_stock(world, owner, bob):
    ids, wh = world["ids"], main_warehouse(world)
    s = supplier(world, owner)
    lines = [
        {"variant_id": ids["tee_s"], "quantity": "10", "unit_cost": "300"},
        {"variant_id": ids["tee_m"], "quantity": "4", "unit_cost": "280.50"},
    ]
    po = admin(
        world, owner, "POST", "purchase-orders", {"supplier_id": s["id"], "lines": lines}
    ).json()
    assert (po["number"], po["status"], po["total"]) == (
        "PO-00001",
        "DRAFT",
        "4122.00",
    )  # 3000 + 1122
    lines[1]["quantity"] = "6"
    po = admin(
        world, owner, "PUT", f"purchase-orders/{po['id']}", {"supplier_id": s["id"], "lines": lines}
    ).json()
    assert po["total"] == "4683.00"
    item = {i["variant_id"]: i["id"] for i in po["items"]}
    receipt = {
        "idempotency_key": "grn-key-0001",
        "warehouse_id": wh,
        "lines": [{"po_item_id": item[ids["tee_s"]], "quantity": "6"}],
    }
    assert (
        admin(world, bob, "POST", f"purchase-orders/{po['id']}/receipts", receipt).json()["error"][
            "code"
        ]
        == "invalid_transition"
    )
    assert (
        admin(world, bob, "POST", f"purchase-orders/{po['id']}/approve").status_code == 403
    )  # staff
    assert (
        admin(world, owner, "POST", f"purchase-orders/{po['id']}/approve").json()["status"]
        == "APPROVED"
    )
    assert (
        admin(
            world,
            owner,
            "PUT",
            f"purchase-orders/{po['id']}",
            {"supplier_id": s["id"], "lines": lines},
        ).status_code
        == 409
    )
    got = admin(
        world, bob, "POST", f"purchase-orders/{po['id']}/receipts", receipt
    ).json()  # staff may receive
    assert got["number"] == "GRN-00001" and got["purchase_order"]["status"] == "PARTIALLY_RECEIVED"
    again = admin(world, bob, "POST", f"purchase-orders/{po['id']}/receipts", receipt).json()
    assert again["replayed"] is True  # the same receipt twice changes nothing
    with Session(world["engine"]) as db:
        b = db.scalar(
            select(InventoryBalance).where(
                InventoryBalance.variant_id == ids["tee_s"], InventoryBalance.warehouse_id == wh
            )
        )
        receipts = db.scalars(
            select(InventoryTransaction.quantity_delta).where(
                InventoryTransaction.txn_type == "RECEIPT"
            )
        ).all()
    # 10 on hand at 250 + 6 at 300 -> 16 at (2500 + 1800) / 16 = 268.75
    assert (b.on_hand, b.avg_cost, receipts) == (16, Decimal("268.7500"), [6])
    over = admin(
        world,
        bob,
        "POST",
        f"purchase-orders/{po['id']}/receipts",
        {
            **receipt,
            "idempotency_key": "grn-key-0002",
            "lines": [{"po_item_id": item[ids["tee_s"]], "quantity": "5"}],
        },
    )
    assert over.status_code == 409 and over.json()["error"]["fields"] == {item[ids["tee_s"]]: "4"}
    rest = admin(
        world,
        bob,
        "POST",
        f"purchase-orders/{po['id']}/receipts",
        {
            **receipt,
            "idempotency_key": "grn-key-0003",
            "lines": [
                {"po_item_id": item[ids["tee_s"]], "quantity": "4"},
                {"po_item_id": item[ids["tee_m"]], "quantity": "6"},
            ],
        },
    ).json()
    assert rest["purchase_order"]["status"] == "RECEIVED"
    assert admin(world, owner, "POST", f"purchase-orders/{po['id']}/cancel").status_code == 409


def test_purchase_order_validation(world, owner):
    s = supplier(world, owner)
    beta_item = [{"variant_id": world["ids"]["beta_variant"], "quantity": "1", "unit_cost": "1"}]
    assert (
        admin(
            world, owner, "POST", "purchase-orders", {"supplier_id": s["id"], "lines": beta_item}
        ).status_code
        == 422
    )
    dup = [{"variant_id": world["ids"]["tee_s"], "quantity": "1", "unit_cost": "1"}] * 2
    assert (
        admin(
            world, owner, "POST", "purchase-orders", {"supplier_id": s["id"], "lines": dup}
        ).status_code
        == 422
    )
    other = supplier(world, owner, "Beta's supplier")
    po = admin(
        world, owner, "POST", "purchase-orders", {"supplier_id": other["id"], "lines": dup[:1]}
    ).json()
    assert (
        admin(world, owner, "POST", f"purchase-orders/{po['id']}/cancel").json()["status"]
        == "CANCELLED"
    )


def upload(world, owner, raw=PDF, name="invoice.pdf"):
    return admin(
        world,
        owner,
        "POST",
        "documents",
        {"filename": name, "content_base64": base64.b64encode(raw).decode()},
    )


def test_documents_are_checked_by_content_and_kept_intact(world, owner):
    doc = upload(world, owner)
    assert doc.status_code == 201 and doc.json()["mime"] == "application/pdf"
    raw = owner.get(
        f"/api/v1/admin/businesses/{world['ids']['acme']}/documents/{doc.json()['id']}/content"
    )
    assert (
        raw.status_code == 200
        and raw.content == PDF
        and "attachment" in raw.headers["content-disposition"]
    )
    assert upload(world, owner, b"MZ\x90\x00 not a pdf", "evil.pdf").status_code == 415  # name lies
    assert upload(world, owner, b"", "empty.pdf").status_code in (413, 415, 422)
    assert upload(world, owner, b"%PDF-" + b"0" * (10 * 1024 * 1024), "big.pdf").status_code == 413
    sneaky = upload(world, owner, PDF, "../../etc/passwd.pdf").json()
    assert sneaky["name"] == "passwd.pdf"


def test_supplier_invoice_review_approval_and_payment(world, owner, bob):
    s = supplier(world, owner)
    doc = upload(world, owner).json()
    pi = admin(
        world,
        owner,
        "POST",
        "purchase-invoices",
        {"document_id": doc["id"], "supplier_id": s["id"]},
    ).json()
    assert pi["status"] == "DRAFT" and set(pi["problems"]) == {
        "supplier_invoice_number",
        "invoice_date",
        "grand_total",
    }
    pi = admin(
        world,
        owner,
        "PATCH",
        f"purchase-invoices/{pi['id']}",
        {
            "supplier_invoice_number": "LT/2611/042",
            "invoice_date": "2026-11-10",
            "taxable_total": "1000.00",
            "tax_total": "50.00",
            "grand_total": "1060.00",
        },
    ).json()
    assert "does not equal" in pi["problems"]["grand_total"]
    assert (
        admin(world, owner, "POST", f"purchase-invoices/{pi['id']}/submit").json()["error"]["code"]
        == "invoice_incomplete"
    )
    pi = admin(
        world, owner, "PATCH", f"purchase-invoices/{pi['id']}", {"grand_total": "1050.00"}
    ).json()
    assert pi["problems"] == {} and len(pi["corrections"]) == 2
    assert pi["corrections"][1]["changes"]["grand_total"] == {"from": "1060.00", "to": "1050.00"}
    assert (
        admin(world, owner, "POST", f"purchase-invoices/{pi['id']}/submit").json()["status"]
        == "NEEDS_REVIEW"
    )
    assert admin(world, bob, "POST", f"purchase-invoices/{pi['id']}/approve").status_code == 403
    approved = admin(world, owner, "POST", f"purchase-invoices/{pi['id']}/approve").json()
    assert approved["status"] == "APPROVED"
    assert (
        admin(world, owner, "POST", f"purchase-invoices/{pi['id']}/approve").status_code == 200
    )  # idempotent
    assert (
        admin(
            world, owner, "PATCH", f"purchase-invoices/{pi['id']}", {"tax_total": "0"}
        ).status_code
        == 409
    )
    dup = admin(
        world,
        owner,
        "POST",
        "purchase-invoices",
        {
            "document_id": doc["id"],
            "supplier_id": s["id"],
            "supplier_invoice_number": "LT/2611/042",
        },
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_invoice"
    payments = admin(world, owner, "GET", "supplier-payments").json()["items"]
    assert len(payments) == 1 and payments[0]["due_date"] == "2026-12-10"  # 30-day terms
    pid = payments[0]["id"]
    assert (
        admin(world, owner, "POST", f"supplier-payments/{pid}/pay", {"amount": "2000"}).json()[
            "error"
        ]["code"]
        == "overpayment"
    )
    part = admin(
        world,
        owner,
        "POST",
        f"supplier-payments/{pid}/pay",
        {"amount": "50", "reference": "NEFT-1"},
    ).json()
    assert (part["status"], part["balance"]) == ("PARTIAL", "1000.00")
    paid = admin(world, owner, "POST", f"supplier-payments/{pid}/pay", {"amount": "1000"}).json()
    assert (paid["status"], paid["balance"]) == ("PAID", "0.00")


def test_purchasing_is_tenant_scoped(world, owner):
    s = supplier(world, owner)
    assert (
        admin(
            world, owner, "PATCH", f"suppliers/{s['id']}", {"name": "Hijack"}, business="beta"
        ).status_code
        == 404
    )
    assert admin(world, owner, "GET", "suppliers", business="beta").json()["items"] == []
