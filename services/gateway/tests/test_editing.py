"""Step 10: editing data. Catalog (categories, products, variants, publishing) and stock
operations (opening stock, adjustments, transfers, reconciliation) with their safety rules."""

import uuid
from decimal import Decimal

import pytest
from app.db.models.supply import InventoryBalance, InventoryTransaction, Warehouse
from conftest import ORIGIN, login, shop
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

SF = "/api/v1/storefront"


@pytest.fixture
def owner(world):
    c = world["client"]
    c.csrf = login(c).json()["csrf_token"]  # alice: OWNER of acme, MANAGER of beta
    return c


def admin(world, client, method, tail, body=None, business="acme"):
    url = f"/api/v1/admin/businesses/{world['ids'][business]}/{tail}"
    headers = {"Origin": ORIGIN} | ({"X-CSRF-Token": client.csrf} if method != "GET" else {})
    return client.request(method, url, json=body if method != "GET" else None, headers=headers)


def warehouses(world, business="acme") -> dict[str, str]:
    with Session(world["engine"]) as s:
        return dict(
            s.execute(
                select(Warehouse.code, Warehouse.id).where(
                    Warehouse.business_id == world["ids"][business]
                )
            ).all()
        )


def balance(world, variant_id, warehouse_id):
    with Session(world["engine"]) as s:
        b = s.scalar(
            select(InventoryBalance).where(
                InventoryBalance.variant_id == variant_id,
                InventoryBalance.warehouse_id == warehouse_id,
            )
        )
        return (b.on_hand, b.reserved, b.avg_cost) if b else None


def new_product(world, owner, **extra):
    body = {
        "name": "Linen Shirt",
        "brand": "Threads",
        "tax_class": "GST_5",
        "hsn_sac": "6205",
        **extra,
    }
    r = admin(world, owner, "POST", "products", body)
    assert r.status_code == 201, r.text
    return r.json()


VARIANT = {
    "sku": "LIN-M-BLU",
    "options": {"size": "M", "Color": "Blue"},
    "selling_price": "1299",
    "mrp": "1499",
    "cost_price": "600",
    "reorder_threshold": "2",
}


# ---- catalog ---------------------------------------------------------------------------------
def test_create_publish_and_sell_a_new_product(world, owner):
    cat = admin(world, owner, "POST", "categories", {"name": "Shirts"}).json()
    p = new_product(world, owner, category_id=cat["id"])
    assert (p["status"], p["slug"]) == ("DRAFT", "linen-shirt")
    r = admin(world, owner, "POST", f"products/{p['id']}/publish")
    assert r.status_code == 422 and r.json()["error"]["fields"] == {
        "variants": "Add at least one active variant with a price."
    }
    v = admin(world, owner, "POST", f"products/{p['id']}/variants", VARIANT).json()["variants"][0]
    assert v["options"] == {"size": "M", "color": "Blue"} and v["cost_price"] == "600.00"
    assert (
        admin(world, owner, "POST", f"products/{p['id']}/publish").json()["status"] == "PUBLISHED"
    )
    shopper = shop(world["gateway"])
    listed = shopper.get(f"{SF}/products?q=linen").json()["items"]
    assert [x["name"] for x in listed] == ["Linen Shirt"] and listed[0][
        "availability"
    ] == "OUT_OF_STOCK"
    wh = warehouses(world)
    r = admin(
        world,
        owner,
        "POST",
        "inventory/opening",
        {"warehouse_id": wh["MAIN"], "lines": [{"variant_id": v["id"], "quantity": "10"}]},
    )
    assert r.status_code == 201, r.text
    assert balance(world, v["id"], wh["MAIN"]) == (10, 0, Decimal("600.0000"))  # catalog cost used
    assert shopper.get(f"{SF}/products/linen-shirt").json()["availability"] == "IN_STOCK"
    again = admin(
        world,
        owner,
        "POST",
        "inventory/opening",
        {"warehouse_id": wh["MAIN"], "lines": [{"variant_id": v["id"], "quantity": "1"}]},
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "has_history"


def test_catalog_validation_rules(world, owner):
    assert (
        admin(world, owner, "POST", "products", {"name": "X", "tax_class": "GST_12"}).status_code
        == 422
    )  # retired
    assert (
        admin(world, owner, "POST", "products", {"name": "X", "hsn_sac": "12"}).status_code == 422
    )
    p = new_product(world, owner)
    twin = new_product(world, owner)
    assert twin["slug"] == "linen-shirt-2"  # names may repeat; web names never collide
    add = lambda body: admin(world, owner, "POST", f"products/{p['id']}/variants", body)  # noqa: E731
    assert add({**VARIANT, "mrp": "999"}).status_code == 422  # MRP below price
    assert add(VARIANT).status_code == 201
    assert add({**VARIANT, "sku": "TEE-S"}).json()["error"]["code"] == "sku_taken"  # existing SKU
    assert (
        add({**VARIANT, "sku": "OTHER", "options": {"color": "blue", "size": "m"}}).json()["error"][
            "code"
        ]
        == "duplicate_options"
    )
    assert add({**VARIANT, "sku": "bad sku!"}).status_code == 422
    assert add({**VARIANT, "sku": "OTHER2", "options": {"Size?": "M"}}).status_code == 422


def test_published_products_keep_a_buyable_variant(world, owner):
    p = new_product(world, owner)
    v = admin(world, owner, "POST", f"products/{p['id']}/variants", VARIANT).json()["variants"][0]
    admin(world, owner, "POST", f"products/{p['id']}/publish")
    last = admin(world, owner, "PATCH", f"variants/{v['id']}", {"is_active": False})
    assert last.status_code == 409 and last.json()["error"]["code"] == "last_variant"
    unit = admin(world, owner, "PATCH", f"products/{p['id']}", {"stock_unit": "KG"})
    assert unit.json()["error"]["code"] == "unit_locked"
    price = admin(world, owner, "PATCH", f"variants/{v['id']}", {"selling_price": "1199"})
    assert price.json()["variants"][0]["price"] == "1199.00"
    admin(world, owner, "POST", f"products/{p['id']}/unpublish")
    assert (
        admin(world, owner, "PATCH", f"variants/{v['id']}", {"archived": True}).status_code == 200
    )
    assert (
        admin(world, owner, "POST", f"products/{p['id']}/publish").status_code == 422
    )  # nothing to sell
    assert admin(world, owner, "POST", f"products/{p['id']}/archive").json()["status"] == "ARCHIVED"
    assert (
        admin(world, owner, "PATCH", f"products/{p['id']}", {"name": "Again"}).json()["error"][
            "code"
        ]
        == "archived"
    )


def test_category_in_use_cannot_be_archived(world, owner):
    cats = {c["slug"]: c for c in admin(world, owner, "GET", "categories").json()["items"]}
    r = admin(world, owner, "PATCH", f"categories/{cats['tops']['id']}", {"archived": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "category_in_use"


def test_editing_needs_permission_and_membership(world, owner):
    bob = TestClient(
        world["gateway"], base_url="http://127.0.0.1:5173", raise_server_exceptions=False
    )
    bob.csrf = login(bob, email="bob@example.com").json()["csrf_token"]  # STAFF: no catalog.write
    assert admin(world, bob, "POST", "products", {"name": "Nope"}).status_code == 403
    dave = TestClient(
        world["gateway"], base_url="http://127.0.0.1:5173", raise_server_exceptions=False
    )
    dave.csrf = login(dave, email="dave@example.com").json()["csrf_token"]
    assert admin(world, dave, "POST", "products", {"name": "Nope"}).status_code == 404
    no_csrf = owner.post(
        f"/api/v1/admin/businesses/{world['ids']['acme']}/products",
        json={"name": "X"},
        headers={"Origin": ORIGIN},
    )
    assert no_csrf.status_code == 403
    beta_variant = world["ids"]["beta_variant"]
    assert (
        admin(world, owner, "PATCH", f"variants/{beta_variant}", {"selling_price": "1"}).status_code
        == 404
    )
    assert admin(world, owner, "PATCH", "variants/..%2F..%2Fx", {}).status_code == 404


# ---- stock operations --------------------------------------------------------------------------
def test_adjustments_counted_and_delta(world, owner):
    ids, wh = world["ids"], warehouses(world)
    r = admin(
        world,
        owner,
        "POST",
        "inventory/adjustments",
        {
            "reason_code": "CYCLE_COUNT",
            "lines": [{"variant_id": ids["tee_m"], "warehouse_id": wh["MAIN"], "counted_qty": "5"}],
        },
    )
    assert r.status_code == 201 and r.json()["lines"][0]["delta"] == "3"  # counted 5, had 2
    assert balance(world, ids["tee_m"], wh["MAIN"])[0] == 5
    below = admin(
        world,
        owner,
        "POST",
        "inventory/adjustments",
        {
            "reason_code": "LOST",
            "lines": [{"variant_id": ids["tee_s"], "warehouse_id": wh["MAIN"], "delta": "-9"}],
        },
    )
    assert (
        below.status_code == 409
        and "held for orders" in below.json()["error"]["fields"][ids["tee_s"]]
    )
    assert balance(world, ids["tee_s"], wh["MAIN"])[0] == 10  # nothing changed
    damaged = admin(
        world,
        owner,
        "POST",
        "inventory/adjustments",
        {
            "reason_code": "DAMAGED",
            "lines": [{"variant_id": ids["tee_s"], "warehouse_id": wh["MAIN"], "delta": "-1"}],
        },
    )
    assert damaged.status_code == 201
    with Session(world["engine"]) as s:
        kinds = s.scalars(
            select(InventoryTransaction.txn_type)
            .where(InventoryTransaction.source_type == "ADJUSTMENT")
            .order_by(InventoryTransaction.id)
        ).all()
    assert kinds == ["ADJUSTMENT", "DAMAGE"]
    both = admin(
        world,
        owner,
        "POST",
        "inventory/adjustments",
        {
            "reason_code": "OTHER",
            "lines": [
                {
                    "variant_id": ids["tee_s"],
                    "warehouse_id": wh["MAIN"],
                    "delta": "1",
                    "counted_qty": "3",
                }
            ],
        },
    )
    assert both.status_code == 422


def test_draft_count_then_post_and_replay(world, owner):
    ids, wh = world["ids"], warehouses(world)
    adj_id = str(uuid.uuid4())
    body = {
        "id": adj_id,
        "reason_code": "CYCLE_COUNT",
        "post": False,
        "lines": [{"variant_id": ids["tee_s"], "warehouse_id": wh["STORE"], "counted_qty": "6"}],
    }
    draft = admin(world, owner, "POST", "inventory/adjustments", body).json()
    assert draft["status"] == "DRAFT" and balance(world, ids["tee_s"], wh["STORE"])[0] == 4
    assert admin(world, owner, "POST", "inventory/adjustments", body).json()["replayed"] is True
    posted = admin(world, owner, "POST", f"inventory/adjustments/{adj_id}/post").json()
    assert posted["status"] == "POSTED" and posted["lines"][0]["delta"] == "2"
    assert balance(world, ids["tee_s"], wh["STORE"])[0] == 6


def test_large_adjustments_need_the_owner(world, owner):
    ids, wh = world["ids"], warehouses(world, "beta")
    body = {
        "reason_code": "FOUND",
        "lines": [{"variant_id": ids["beta_variant"], "warehouse_id": wh["MAIN"], "delta": "150"}],
    }
    r = admin(
        world, owner, "POST", "inventory/adjustments", body, business="beta"
    )  # alice is MANAGER of beta
    assert r.status_code == 403 and r.json()["error"]["code"] == "large_adjustment"
    wh_acme = warehouses(world)
    ok = admin(
        world,
        owner,
        "POST",
        "inventory/adjustments",
        {
            **body,
            "lines": [
                {"variant_id": ids["tee_s"], "warehouse_id": wh_acme["MAIN"], "delta": "150"}
            ],
        },
    )
    assert ok.status_code == 201  # the OWNER may


def test_transfer_dispatch_receive_and_cancel(world, owner):
    ids, wh = world["ids"], warehouses(world)
    too_many = admin(
        world,
        owner,
        "POST",
        "inventory/transfers",
        {
            "from_warehouse_id": wh["MAIN"],
            "to_warehouse_id": wh["STORE"],
            "lines": [{"variant_id": ids["tee_s"], "quantity": "9"}],
        },
    )
    assert too_many.status_code == 409 and too_many.json()["error"]["fields"] == {
        ids["tee_s"]: "8"
    }  # 2 are held
    same = admin(
        world,
        owner,
        "POST",
        "inventory/transfers",
        {
            "from_warehouse_id": wh["MAIN"],
            "to_warehouse_id": wh["MAIN"],
            "lines": [{"variant_id": ids["tee_s"], "quantity": "1"}],
        },
    )
    assert same.status_code == 422
    t = admin(
        world,
        owner,
        "POST",
        "inventory/transfers",
        {
            "from_warehouse_id": wh["MAIN"],
            "to_warehouse_id": wh["STORE"],
            "lines": [{"variant_id": ids["tee_s"], "quantity": "5"}],
        },
    ).json()
    assert t["status"] == "IN_TRANSIT" and balance(world, ids["tee_s"], wh["MAIN"])[0] == 5
    assert balance(world, ids["tee_s"], wh["STORE"])[0] == 4  # not there yet
    got = admin(world, owner, "POST", f"inventory/transfers/{t['id']}/receive").json()
    assert got["status"] == "RECEIVED" and balance(world, ids["tee_s"], wh["STORE"])[0] == 9
    assert admin(world, owner, "POST", f"inventory/transfers/{t['id']}/cancel").status_code == 409
    back = admin(
        world,
        owner,
        "POST",
        "inventory/transfers",
        {
            "from_warehouse_id": wh["STORE"],
            "to_warehouse_id": wh["MAIN"],
            "lines": [{"variant_id": ids["tee_s"], "quantity": "2"}],
        },
    ).json()
    admin(world, owner, "POST", f"inventory/transfers/{back['id']}/cancel")
    assert balance(world, ids["tee_s"], wh["STORE"])[0] == 9  # cancelled in transit: back at source


def test_reconciliation_reports_without_fixing(world, owner):
    run = admin(world, owner, "POST", "inventory/reconcile").json()
    # the fixture holds 2 TEE-S in MAIN without a reservation row: reported, not changed
    kinds = {(d["kind"], d["expected"], d["actual"]) for d in run["discrepancies"]}
    assert ("RESERVED_MISMATCH", "0", "2") in kinds
    assert balance(world, world["ids"]["tee_s"], warehouses(world)["MAIN"])[1] == 2
    assert admin(world, owner, "GET", "inventory/reconcile").json()["items"][0]["id"] == run["id"]


def test_new_warehouse(world, owner):
    r = admin(
        world, owner, "POST", "warehouses", {"code": "east", "name": "East hub", "state_code": "29"}
    )
    assert r.status_code == 201 and r.json()["code"] == "EAST" and not r.json()["is_default"]
    assert (
        admin(world, owner, "POST", "warehouses", {"code": "EAST", "name": "Dup"}).status_code
        == 409
    )
