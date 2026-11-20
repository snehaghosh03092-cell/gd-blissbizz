"""Step 12: a new owner signs up, completes the guided details (GSTIN checked), sets up the shop
and goes live; the new shop is then reachable at <slug>.localhost."""

import pytest
from app.core.gstin import with_checksum
from conftest import ORIGIN, PASSWORD, bootstrap, login, shop
from fastapi.testclient import TestClient

GSTIN_KA = with_checksum("29AAACN1234B1Z")  # Karnataka
GSTIN_MH = with_checksum("27AAACN1234B1Z")  # Maharashtra
SIGNUP = {
    "full_name": "Nina Owner",
    "email": "nina@example.com",
    "password": PASSWORD,
    "business_name": "Nina's Knits",
    "slug": "ninas-knits",
}


def client(world):
    return TestClient(
        world["gateway"],
        base_url="http://127.0.0.1:5173",
        raise_server_exceptions=False,
        headers={"Origin": ORIGIN},
    )


@pytest.fixture
def nina(world):
    c = client(world)
    csrf = bootstrap(c)
    r = c.post("/api/v1/auth/register", json=SIGNUP, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201, r.text
    c.csrf = r.json()["csrf_token"]
    c.bid = r.json()["business"]["id"]
    return c


def call(c, method, tail, body=None):
    headers = {"X-CSRF-Token": c.csrf} if method != "GET" else {}
    return c.request(method, f"/api/v1/admin/businesses/{c.bid}/{tail}", json=body, headers=headers)


def test_signup_creates_owner_business_and_session(world, nina):
    me = nina.get("/api/v1/auth/me").json()
    assert me["owner"]["email"] == "nina@example.com"
    assert me["businesses"] == [
        {
            "id": nina.bid,
            "name": "Nina's Knits",
            "slug": "ninas-knits",
            "role": "OWNER",
            "status": "ONBOARDING",
        }
    ]
    detail = nina.get(f"/api/v1/admin/businesses/{nina.bid}").json()
    assert "storefront.publish" in detail["permissions"]


def test_signup_rules(world, nina):
    c = client(world)
    assert c.get("/api/v1/auth/slug?value=ninas-knits").json()["available"] is False
    assert c.get("/api/v1/auth/slug?value=Brand New Shop").json() == {
        "slug": "brand-new-shop",
        "available": True,
        "reason": None,
    }
    csrf = bootstrap(c)
    for change, code in (
        ({"slug": "acme"}, "slug_unavailable"),
        ({"email": "ALICE@example.com", "slug": "x-new"}, "email_unavailable"),
        ({"password": "short", "slug": "x-new2"}, "validation_error"),
        ({"slug": "a"}, "validation_error"),
    ):
        r = c.post(
            "/api/v1/auth/register",
            json={**SIGNUP, "email": "candidate@example.com", **change},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.json()["error"]["code"] == code, (change, r.text)
    assert c.post("/api/v1/auth/register", json=SIGNUP).status_code == 403  # no CSRF token
    assert (
        nina.post(
            "/api/v1/auth/register",
            json={**SIGNUP, "slug": "another-one", "email": "x@example.com"},
            headers={"X-CSRF-Token": nina.csrf},
        ).status_code
        == 409
    )  # already signed in


def test_guided_details_ask_only_what_is_missing(world, nina):
    first = call(nina, "GET", "onboarding").json()
    assert first["state"] == "DRAFT" and first["missing"] == [
        "category",
        "state_code",
        "city",
        "sales_mode",
    ]
    assert len(first["next_questions"]) == 2 and first["next_questions"][0].startswith(
        "What do you sell"
    )
    step = call(
        nina,
        "PATCH",
        "onboarding",
        {"category": "clothing", "state_code": "29", "city": "Bengaluru"},
    ).json()
    assert step["state"] == "COLLECTING_DETAILS" and step["missing"] == ["sales_mode"]
    assert len(step["next_questions"]) == 1  # answered questions are never asked again
    assert (
        call(nina, "PATCH", "onboarding", {"pan": "ABCDE1234F"}).status_code == 422
    )  # never asked, never accepted
    assert call(nina, "PATCH", "onboarding", {"state_code": "99"}).status_code == 422
    assert call(nina, "PATCH", "onboarding", {"postal_code": "012345"}).status_code == 422
    early = call(nina, "POST", "onboarding/submit")
    assert early.status_code == 422 and list(early.json()["error"]["fields"]) == ["sales_mode"]
    assert call(nina, "GET", "onboarding").json()["state"] == "NEEDS_CORRECTION"
    call(nina, "PATCH", "onboarding", {"sales_mode": "ONLINE", "postal_code": "560001"})
    ready = call(nina, "POST", "onboarding/submit").json()
    assert ready["state"] == "READY_FOR_STORE_SETUP" and ready["complete"]


def test_gstin_rules(world, nina):
    call(nina, "PATCH", "onboarding", {"state_code": "29"})
    no_number = call(nina, "PATCH", "onboarding", {"gst_registered": True})
    assert no_number.status_code == 422 and "gstin" in no_number.json()["error"]["fields"]
    other_state = call(nina, "PATCH", "onboarding", {"gst_registered": True, "gstin": GSTIN_MH})
    assert "Maharashtra" in other_state.json()["error"]["fields"]["gstin"]
    typo = call(
        nina,
        "PATCH",
        "onboarding",
        {"gst_registered": True, "gstin": GSTIN_KA[:-1] + ("A" if GSTIN_KA[-1] != "A" else "B")},
    )
    assert "typo" in typo.json()["error"]["fields"]["gstin"]
    ok = call(
        nina, "PATCH", "onboarding", {"gst_registered": True, "gstin": GSTIN_KA.lower()}
    ).json()["fields"]
    assert (ok["gst_registered"], ok["gstin"], ok["gstin_status"]) == (
        True,
        GSTIN_KA,
        "FORMAT_VALID",
    )
    moved = call(
        nina, "PATCH", "onboarding", {"state_code": "27"}
    )  # GSTIN no longer matches the state
    assert moved.status_code == 422
    tool = nina.post(
        "/api/v1/tools/gstin-check", json={"gstin": GSTIN_KA, "state_code": "29"}
    ).json()
    assert (
        tool["ok"]
        and tool["status"] == "FORMAT_VALID"
        and "not a government verification" in tool["message"]
    )


def complete_details(c):
    call(
        c,
        "PATCH",
        "onboarding",
        {
            "category": "clothing",
            "state_code": "29",
            "city": "Bengaluru",
            "sales_mode": "ONLINE",
            "gst_registered": True,
            "gstin": GSTIN_KA,
        },
    )
    assert call(c, "POST", "onboarding/submit").json()["state"] == "READY_FOR_STORE_SETUP"


def test_going_live_in_order_and_shop_appears(world, nina):
    early = call(nina, "POST", "storefront/publish")
    assert early.json()["error"]["code"] == "onboarding_incomplete"
    complete_details(nina)
    assert call(nina, "POST", "storefront/publish").json()["error"]["code"] == "warehouse_missing"
    assert (
        call(
            nina, "POST", "warehouses", {"code": "main", "name": "Home studio", "state_code": "29"}
        ).status_code
        == 201
    )
    assert call(nina, "POST", "storefront/publish").json()["error"]["code"] == "store_not_set_up"
    setup = call(
        nina,
        "PUT",
        "storefront/setup",
        {"template_key": "fashion", "hero_title": "Hand-knitted warmth"},
    ).json()
    assert (
        setup["storefront"]["status"] == "DRAFT"
        and setup["storefront"]["theme"]["primary"] == "#1f2937"
    )
    assert call(nina, "POST", "storefront/publish").json()["error"]["code"] == "policies_missing"
    bad = call(nina, "PUT", "policies", {"shipping_days_min": 7, "shipping_days_max": 2})
    assert bad.status_code == 422
    call(
        nina,
        "PUT",
        "policies",
        {
            "shipping_days_min": 2,
            "shipping_days_max": 5,
            "free_shipping_over": "999",
            "flat_shipping_fee": "59",
            "return_window_days": 10,
            "cancel_window_hours": 12,
        },
    )
    assert (
        shop(world["gateway"], "ninas-knits.localhost").get("/api/v1/storefront").status_code == 404
    )  # not yet
    live = call(nina, "POST", "storefront/publish")
    assert live.status_code == 200, live.text
    assert live.json()["shop_url_host"] == "ninas-knits.localhost"
    assert nina.get("/api/v1/auth/me").json()["businesses"][0]["status"] == "ACTIVE"
    assert call(nina, "GET", "onboarding").json()["state"] == "ACTIVE"
    store = shop(world["gateway"], "ninas-knits.localhost").get("/api/v1/storefront").json()
    assert (
        store["business"]["name"] == "Nina's Knits"
        and store["config"]["hero"]["title"] == "Hand-knitted warmth"
    )
    assert [d["slug"] for d in store["documents"]] == ["returns", "shipping"]
    assert call(nina, "POST", "storefront/publish").status_code == 200  # again: nothing breaks
    draft = call(
        nina,
        "PUT",
        "storefront/setup",
        {"template_key": "beauty", "hero_title": "The new collection"},
    ).json()
    assert draft["storefront"]["unpublished_changes"]
    public = shop(world["gateway"], "ninas-knits.localhost")
    assert public.get("/api/v1/storefront").json()["template_key"] == "fashion"
    assert call(nina, "POST", "storefront/publish").status_code == 200
    published = public.get("/api/v1/storefront").json()
    assert published["template_key"] == "beauty"
    assert published["theme"]["primary"] == "#65374e"
    assert published["config"]["hero"]["title"] == "The new collection"


def test_active_shop_cannot_lose_required_details_and_managers_cannot_publish(world):
    c = client(world)
    c.csrf = login(c).json()[
        "csrf_token"
    ]  # alice: OWNER of acme (seeded as active), MANAGER of beta
    c.bid = world["ids"]["acme"]
    call(
        c,
        "PATCH",
        "onboarding",
        {"category": "clothing", "city": "Bengaluru", "sales_mode": "HYBRID"},
    )
    wiped = call(c, "PATCH", "onboarding", {"category": None})
    assert wiped.status_code == 422
    c.bid = world["ids"]["beta"]
    assert (
        call(c, "POST", "storefront/publish").status_code == 403
    )  # managers lack storefront.publish
    assert call(c, "PATCH", "onboarding", {"city": "Pune"}).status_code == 403  # and business.write
