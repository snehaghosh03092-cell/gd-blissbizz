"""Catalog and stock through the gateway: isolation, permissions, composition, degradation."""

import httpx
import pytest
from blissbiz_common.context import InternalContext
from blissbiz_common.permissions import permissions_for
from conftest import login
from fastapi.testclient import TestClient

ROUTES = ["categories", "products", "warehouses", "inventory", "inventory/ledger?variant_id=x"]


def ctx_for(business_id, role="OWNER", permissions=None):
    perms = permissions_for(role) if permissions is None else permissions
    return InternalContext("req-12345678", "OWNER", business_id, "owner-1", role, tuple(perms))


@pytest.fixture
def owner(world):
    login(world["client"])  # alice: OWNER of acme, MANAGER of beta
    return world


def url(world, key, tail):
    return f"/api/v1/admin/businesses/{world['ids'][key]}/{tail}"


# ---- products ------------------------------------------------------------------------
def test_product_list_with_price_ranges_and_variant_counts(owner):
    r = owner["client"].get(url(owner, "acme", "products")).json()
    assert r["total"] == 4 and [p["name"] for p in r["items"]] == [
        "Hat",
        "Old Coat",
        "Scarf",
        "Tee",
    ]
    tee = next(p for p in r["items"] if p["name"] == "Tee")
    assert tee["variants"] == 2 and tee["price_min"] == tee["price_max"] == "499.00"
    assert tee["category"] == "Tops" and tee["status"] == "PUBLISHED"


def test_product_filters_and_paging(owner):
    c = owner["client"]
    assert [
        p["name"] for p in c.get(url(owner, "acme", "products?status=DRAFT")).json()["items"]
    ] == ["Hat"]
    assert c.get(url(owner, "acme", "products?category=hats")).json()["total"] == 2
    assert [p["name"] for p in c.get(url(owner, "acme", "products?q=tee")).json()["items"]] == [
        "Tee"
    ]
    page2 = c.get(url(owner, "acme", "products?page=2&page_size=3")).json()
    assert page2["total"] == 4 and len(page2["items"]) == 1


def test_search_text_is_matched_literally(owner):
    assert (
        owner["client"].get(url(owner, "acme", "products?q=%25")).json()["total"] == 0
    )  # '%' is not a wildcard


@pytest.mark.parametrize("query", ["status=BOGUS", "page=0", "page_size=1000", "page_size=0"])
def test_bad_list_parameters_are_rejected(owner, query):
    assert owner["client"].get(url(owner, "acme", f"products?{query}")).status_code == 422


def test_product_detail_merges_stock_from_supply(owner):
    d = owner["client"].get(url(owner, "acme", f"products/{owner['ids']['tee']}")).json()
    assert d["stock_unavailable"] is False
    s_variant = next(v for v in d["variants"] if v["sku"] == "TEE-S")
    assert s_variant["stock"]["available"] == "12" and s_variant["stock"]["reserved"] == "2"
    assert [w["code"] for w in s_variant["stock"]["warehouses"]] == ["MAIN", "STORE"]
    assert s_variant["cost_price"] == "250.00"  # owners may see cost


def test_staff_do_not_see_cost_prices(world):
    login(world["client"], email="bob@example.com")  # STAFF: catalog.read but not catalog.write
    d = world["client"].get(url(world, "acme", f"products/{world['ids']['tee']}")).json()
    assert d["variants"] and all("cost_price" not in v for v in d["variants"])


def test_categories_count_non_archived_products(owner):
    items = {
        c["slug"]: c["products"]
        for c in owner["client"].get(url(owner, "acme", "categories")).json()["items"]
    }
    assert items == {"tops": 1, "hats": 2}  # the archived coat is not counted


# ---- tenant isolation ---------------------------------------------------------------
def test_another_tenants_product_is_404_even_for_a_member_of_both(owner):
    c, ids = owner["client"], owner["ids"]
    assert c.get(url(owner, "acme", f"products/{ids['beta_product']}")).status_code == 404
    assert c.get(url(owner, "beta", f"products/{ids['beta_product']}")).status_code == 200
    assert c.get(url(owner, "beta", f"products/{ids['tee']}")).status_code == 404  # and the reverse


def test_inventory_never_mixes_businesses(owner):
    acme = owner["client"].get(url(owner, "acme", "inventory?page_size=100")).json()
    beta = owner["client"].get(url(owner, "beta", "inventory")).json()
    assert "BETA-1" not in {r["sku"] for r in acme["items"]}
    assert [r["sku"] for r in beta["items"]] == ["BETA-1"] and beta["items"][0]["available"] == "7"


@pytest.mark.parametrize("route", ROUTES)
def test_new_routes_need_a_session_and_a_membership(world, route):
    c, acme = world["client"], world["ids"]["acme"]
    assert c.get(f"/api/v1/admin/businesses/{acme}/{route}").status_code == 401
    login(c, email="dave@example.com")  # belongs only to the legacy business
    assert c.get(f"/api/v1/admin/businesses/{acme}/{route}").status_code == 404


# ---- inventory composition ----------------------------------------------------------
def test_inventory_joins_names_with_stock_and_classifies_each_variant(owner):
    r = owner["client"].get(url(owner, "acme", "inventory?page_size=100")).json()
    by_sku = {i["sku"]: i for i in r["items"]}
    statuses = {k: v["status"] for k, v in by_sku.items()}
    assert statuses == {
        "TEE-S": "OK",
        "TEE-M": "LOW",
        "SCARF-1": "OUT",
        "HAT-1": "NOT_FOR_SALE",
        "OLD-1": "NOT_FOR_SALE",
    }
    assert by_sku["TEE-S"]["product_name"] == "Tee" and by_sku["TEE-S"]["available"] == "12"
    assert by_sku["OLD-1"]["available"] == "5"  # archived stock is still reported
    assert by_sku["HAT-1"]["available"] == "0"  # a variant with no stock rows is zero, not missing
    assert r["summary"] == {"variants": 5, "out_of_stock": 1, "low_stock": 2}


def test_low_and_out_filters_follow_the_read_view_rules(owner):
    c = owner["client"]
    low = c.get(url(owner, "acme", "inventory?low_only=true")).json()
    assert sorted(i["sku"] for i in low["items"]) == ["SCARF-1", "TEE-M"] and low["total"] == 2
    out = c.get(url(owner, "acme", "inventory?out_of_stock=true")).json()
    assert [i["sku"] for i in out["items"]] == ["SCARF-1"]
    assert [i["sku"] for i in c.get(url(owner, "acme", "inventory?q=tee")).json()["items"]] == [
        "TEE-M",
        "TEE-S",
    ]


def test_inventory_paging_is_stable(owner):
    c = owner["client"]
    p1 = c.get(url(owner, "acme", "inventory?page_size=2&page=1")).json()
    p2 = c.get(url(owner, "acme", "inventory?page_size=2&page=2")).json()
    p3 = c.get(url(owner, "acme", "inventory?page_size=2&page=3")).json()
    skus = [i["sku"] for p in (p1, p2, p3) for i in p["items"]]
    assert p1["total"] == 5 and len(skus) == 5 and len(set(skus)) == 5


def test_ledger_shows_history_and_cannot_reach_other_tenants(owner):
    c, ids = owner["client"], owner["ids"]
    r = c.get(url(owner, "acme", f"inventory/ledger?variant_id={ids['tee_s']}")).json()
    assert r["total"] == 2 and {x["warehouse"] for x in r["items"]} == {"MAIN", "STORE"}
    assert all(x["type"] == "OPENING" and x["reason"] == "OPENING_STOCK" for x in r["items"])
    foreign = c.get(url(owner, "acme", f"inventory/ledger?variant_id={ids['beta_variant']}")).json()
    assert foreign["total"] == 0 and foreign["items"] == []
    assert (
        c.get(url(owner, "acme", "inventory/ledger")).status_code == 422
    )  # variant_id is required


# ---- permissions enforced by the services themselves ------------------------------------
def test_services_enforce_permissions_from_the_signed_context(world):
    acme = world["ids"]["acme"]
    blind = ctx_for(acme, permissions=())
    assert internal_status(world["catalog"], "/internal/v1/products", blind) == 403
    assert internal_status(world["supply"], "/internal/v1/warehouses", blind) == 403
    assert internal_status(world["catalog"], "/internal/v1/products", ctx_for(acme)) == 200


def test_stock_costs_are_visible_only_with_inventory_manage(world):
    from conftest import internal_call

    acme, v = world["ids"]["acme"], world["ids"]["tee_s"]
    body = {"variant_ids": [v]}
    _, owner_view = internal_call(
        world["supply"], "POST", "/internal/v1/stock/query", ctx_for(acme), body
    )
    _, staff_view = internal_call(
        world["supply"], "POST", "/internal/v1/stock/query", ctx_for(acme, "STAFF"), body
    )
    assert owner_view["items"][v]["stock_value"] == "3500.00"  # 14 units at 250
    assert "stock_value" not in staff_view["items"][v]


def test_stock_query_for_another_tenants_variant_returns_nothing(world):
    from conftest import internal_call

    ids = world["ids"]
    ctx, body = ctx_for(ids["acme"]), {"variant_ids": [ids["beta_variant"]]}
    _, data = internal_call(world["supply"], "POST", "/internal/v1/stock/query", ctx, body)
    assert (
        data["items"][ids["beta_variant"]]["available"] == "0"
    )  # beta's 7 units are invisible to acme


def test_stock_query_limits_batch_size(world):
    from conftest import internal_call

    ctx, body = ctx_for(world["ids"]["acme"]), {"variant_ids": [str(i) for i in range(201)]}
    status, _ = internal_call(world["supply"], "POST", "/internal/v1/stock/query", ctx, body)
    assert status == 422


# ---- resilience ----------------------------------------------------------------------
def refusing(request):
    raise httpx.ConnectError("refused")


def test_product_detail_degrades_when_supply_is_down(world):
    gateway = world["make_gateway"](supply_transport=httpx.MockTransport(refusing))
    client = TestClient(gateway, base_url="http://127.0.0.1:5173", raise_server_exceptions=False)
    login(client)
    r = client.get(url(world, "acme", f"products/{world['ids']['tee']}"))
    assert r.status_code == 200 and r.json()["stock_unavailable"] is True
    assert all(v["stock"] is None for v in r.json()["variants"])
    assert (
        client.get(url(world, "acme", "inventory")).status_code == 503
    )  # cannot be composed without stock
    assert (
        client.get(url(world, "acme", "products")).status_code == 200
    )  # catalog alone is unaffected


def test_readiness_names_the_service_that_is_down(world):
    gateway = world["make_gateway"](catalog_transport=httpx.MockTransport(refusing))
    client = TestClient(gateway, base_url="http://127.0.0.1:5173", raise_server_exceptions=False)
    r = client.get("/api/ready")
    assert r.status_code == 503 and "catalog" in r.json()["error"]["message"]
    assert "identity" not in r.json()["error"]["message"]


def internal_status(app, path, ctx):
    from conftest import internal_call

    return internal_call(app, "GET", path, ctx)[0]
