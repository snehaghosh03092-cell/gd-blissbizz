"""Steps 4-5: a shop is found by its hostname; shoppers browse only published, in-stock-aware
products; customer accounts are per shop; addresses belong to their customer only."""

import pytest
from conftest import PASSWORD, login, shop, shopper_csrf, shopper_login

SF = "/api/v1/storefront"


@pytest.fixture
def acme(world):
    return shop(world["gateway"])


# ---- step 4: which shop, and what it shows ----------------------------------------------
def test_store_info_comes_from_the_host(acme):
    r = acme.get(SF)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["business"]["name"] == "Acme" and body["template_key"] == "fashion"
    assert body["policies"]["free_shipping_over"] == "999.00"
    assert [d["slug"] for d in body["documents"]] == ["returns"]  # the OWNER handbook stays private


@pytest.mark.parametrize(
    "host", ["nowhere.localhost", "old-acme.localhost", "legacy.localhost", "gone.localhost"]
)
def test_unknown_removed_draft_or_suspended_shops_are_not_found(world, host):
    """Unknown host, REMOVED domain, DRAFT storefront, SUSPENDED business: all look the same."""
    r = shop(world["gateway"], host).get(f"{SF}/products")
    assert r.status_code == 404 and r.json()["error"]["code"] == "store_not_found"


def test_host_matching_ignores_port_and_case(world):
    c = shop(world["gateway"], "ACME.localhost:8000")
    assert c.get(SF, headers={"Origin": "http://acme.localhost:8000"}).status_code == 200


def test_another_shops_origin_is_rejected(acme):
    r = acme.get(f"{SF}/products", headers={"Origin": "http://beta.localhost"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_rejected"


def test_only_published_products_with_live_stock(acme):
    r = acme.get(f"{SF}/products").json()
    assert [p["name"] for p in r["items"]] == ["Tee", "Scarf"]  # in-stock first; no draft/archived
    tee, scarf = r["items"]
    assert tee["in_stock"] and tee["availability"] == "IN_STOCK" and tee["price_min"] == "499.00"
    assert scarf["availability"] == "OUT_OF_STOCK" and not scarf["in_stock"]
    by_sku = {v["sku"]: v for v in tee["variants"]}
    assert (
        by_sku["TEE-S"]["availability"] == "IN_STOCK"
        and "available_quantity" not in by_sku["TEE-S"]
    )
    assert by_sku["TEE-M"] == by_sku["TEE-M"] | {
        "availability": "LOW_STOCK",
        "available_quantity": "2",
    }


def test_no_internal_fields_leak_to_shoppers(acme):
    text = acme.get(f"{SF}/products").text + acme.get(f"{SF}/products/tee").text
    for secret in ("cost_price", "reorder_threshold", "avg_cost", "stock_value", "warehouse"):
        assert secret not in text


def test_in_stock_filter_and_sorting(acme):
    assert [p["name"] for p in acme.get(f"{SF}/products?in_stock=true").json()["items"]] == ["Tee"]
    names = [p["name"] for p in acme.get(f"{SF}/products?sort=name").json()["items"]]
    assert names == ["Scarf", "Tee"]
    assert acme.get(f"{SF}/products?sort=cheapest").status_code == 422


@pytest.mark.parametrize(
    "query, expected",
    [
        ("color=black", {"Tee": ["TEE-S"]}),
        ("color=BLACK&size=m", {}),
        ("size=m", {"Tee": ["TEE-M"]}),
        ("q=black%20tee", {"Tee": ["TEE-S"]}),  # a word can match an option value
        ("q=tees", {"Tee": ["TEE-M", "TEE-S"]}),  # plural folding (variants listed by SKU)
        ("q=threads%20tops", {"Tee": ["TEE-M", "TEE-S"]}),  # brand + category words
        ("q=hat", {"Scarf": ["SCARF-1"]}),  # Scarf is in "Hats"; the draft Hat never shows
        ("category=hats", {"Scarf": ["SCARF-1"]}),
        ("max_price=100", {}),
        ("min_price=499&max_price=499&color=white", {"Tee": ["TEE-M"]}),
    ],
)
def test_search_filters_variants(acme, query, expected):
    items = acme.get(f"{SF}/products?{query}").json()["items"]
    assert {p["name"]: [v["sku"] for v in p["variants"]] for p in items} == expected


def test_paging(acme):
    r = acme.get(f"{SF}/products?page=2&page_size=1").json()
    assert r["total"] == 2 and [p["name"] for p in r["items"]] == ["Scarf"]


def test_product_detail_by_slug_and_isolation(world, acme):
    assert acme.get(f"{SF}/products/tee").json()["description"] is None
    assert acme.get(f"{SF}/products/hat").status_code == 404  # draft
    assert acme.get(f"{SF}/products/old-coat").status_code == 404  # archived
    assert acme.get(f"{SF}/products/beta-widget").status_code == 404  # another shop's product
    beta = shop(world["gateway"], "beta.localhost")
    widget = beta.get(f"{SF}/products/beta-widget").json()
    assert widget["available_quantity"] == "7"  # beta allows exact stock counts


def test_categories_count_only_visible_products(acme):
    assert acme.get(f"{SF}/categories").json()["items"] == [
        {"name": "Hats", "slug": "hats", "products": 1},
        {"name": "Tops", "slug": "tops", "products": 1},
    ]


# ---- step 5: customer accounts -----------------------------------------------------------
def register(client, email="dan@example.com", password=PASSWORD, **extra):
    csrf = shopper_csrf(client)
    body = {"email": email, "password": password, "full_name": "Dan Buyer", **extra}
    return client.post(f"{SF}/auth/register", json=body, headers={"X-CSRF-Token": csrf})


def test_register_signs_in_and_me_works(acme):
    r = register(acme, phone="9811111111")
    assert r.status_code == 201, r.text
    assert r.json()["customer"]["email"] == "dan@example.com"
    me = acme.get(f"{SF}/auth/me")
    assert me.status_code == 200 and me.json()["customer"]["full_name"] == "Dan Buyer"


def test_register_rules(world, acme):
    assert register(acme, password="short").status_code == 422
    assert register(acme, email="not-an-email").status_code == 422
    dup = register(shop(world["gateway"]), email="CARA@example.com")
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "email_unavailable"
    r = acme.post(
        f"{SF}/auth/register",
        json={"email": "x@example.com", "password": PASSWORD, "full_name": "X"},
    )
    assert r.status_code == 403  # no CSRF token


def test_same_email_is_a_different_account_in_each_shop(world):
    a, b = shop(world["gateway"]), shop(world["gateway"], "beta.localhost")
    shopper_login(a)
    shopper_login(b)
    assert a.get(f"{SF}/auth/me").json()["customer"]["full_name"] == "Cara Shopper"
    assert b.get(f"{SF}/auth/me").json()["customer"]["full_name"] == "Cara at Beta"


def test_a_customer_cookie_does_not_work_in_another_shop_or_admin(world, acme):
    shopper_login(acme)
    stolen = acme.cookies.get("blissbiz_session")
    beta = shop(world["gateway"], "beta.localhost")
    beta.cookies.set("blissbiz_session", stolen)
    assert beta.get(f"{SF}/auth/me").status_code == 401  # treated as anonymous on beta
    admin = world["client"]
    admin.cookies.set("blissbiz_session", stolen)
    assert admin.get("/api/v1/auth/me").status_code == 401  # and never as an owner


def test_bad_credentials_look_the_same(acme):
    csrf = shopper_csrf(acme)
    for email, password in (
        ("cara@example.com", "Wrong-password-123!"),
        ("nobody@example.com", PASSWORD),
    ):
        r = acme.post(
            f"{SF}/auth/login",
            json={"email": email, "password": password},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 401 and r.json()["error"]["message"] == "Invalid email or password."


def test_owner_cannot_use_storefront_login_while_signed_in(world):
    c = world["client"]
    assert login(c).status_code == 200  # alice, owner session on the admin host
    shopper = shop(world["gateway"])
    shopper.cookies.set("blissbiz_session", c.cookies.get("blissbiz_session"))
    csrf = c.get("/api/v1/auth/csrf", headers={"Origin": "http://127.0.0.1:5173"}).json()[
        "csrf_token"
    ]
    r = shopper.post(
        f"{SF}/auth/login",
        json={"email": "cara@example.com", "password": PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409


def test_owner_can_sign_in_from_their_storefront_and_open_workspace(world):
    storefront = shop(world["gateway"])
    csrf = shopper_csrf(storefront)
    r = storefront.post(
        f"{SF}/auth/login",
        json={"email": "bob@example.com", "password": PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    assert r.json()["owner"]["email"] == "bob@example.com"
    assert r.json()["business"]["id"] == world["ids"]["acme"]
    me = storefront.get(f"{SF}/auth/me")
    assert me.status_code == 200 and me.json()["actor_type"] == "owner"
    workspace = storefront.get(f"/api/v1/admin/businesses/{world['ids']['acme']}")
    assert workspace.status_code == 200 and workspace.json()["role"] == "STAFF"


def test_owner_cannot_sign_in_through_an_unrelated_storefront(world):
    storefront = shop(world["gateway"], "beta.localhost")
    csrf = shopper_csrf(storefront)
    r = storefront.post(
        f"{SF}/auth/login",
        json={"email": "bob@example.com", "password": PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "Invalid email or password."


def test_logout(acme):
    csrf = shopper_login(acme)
    assert acme.post(f"{SF}/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert acme.get(f"{SF}/auth/me").status_code == 401


# ---- step 5: addresses -------------------------------------------------------------------
ADDRESS = {
    "recipient": "Cara",
    "phone": "9800000001",
    "line1": "5 Park St",
    "city": "Kolkata",
    "state_code": "19",
    "postal_code": "700016",
}


def test_addresses_crud_and_validation(acme):
    csrf = shopper_login(acme)
    h = {"X-CSRF-Token": csrf}
    items = acme.get(f"{SF}/me/addresses").json()["items"]
    assert [a["city"] for a in items] == ["Bengaluru", "Pune"] and items[0]["is_default"]
    bad_state = acme.post(f"{SF}/me/addresses", json={**ADDRESS, "state_code": "99"}, headers=h)
    assert bad_state.json()["error"]["fields"] == {"state_code": "Value error, Unknown state code."}
    bad_pin = acme.post(f"{SF}/me/addresses", json={**ADDRESS, "postal_code": "012345"}, headers=h)
    extra = acme.post(f"{SF}/me/addresses", json={**ADDRESS, "customer_id": "x"}, headers=h)
    assert (bad_state.status_code, bad_pin.status_code, extra.status_code) == (422, 422, 422)
    made = acme.post(f"{SF}/me/addresses", json={**ADDRESS, "is_default": True}, headers=h)
    assert made.status_code == 201 and made.json()["state"] == "West Bengal"
    items = acme.get(f"{SF}/me/addresses").json()["items"]
    assert items[0]["city"] == "Kolkata" and sum(a["is_default"] for a in items) == 1
    assert acme.delete(f"{SF}/me/addresses/{made.json()['id']}", headers=h).status_code == 204
    items = acme.get(f"{SF}/me/addresses").json()["items"]
    assert len(items) == 2 and sum(a["is_default"] for a in items) == 1


def test_addresses_are_private(world, acme):
    assert acme.get(f"{SF}/me/addresses").status_code == 401  # anonymous
    register(acme)  # dan, another acme customer
    csrf = shopper_csrf(acme)
    home = world["ids"]["home"]
    assert (
        acme.delete(f"{SF}/me/addresses/{home}", headers={"X-CSRF-Token": csrf}).status_code == 404
    )
    assert acme.get(f"{SF}/me/addresses").json()["items"] == []
