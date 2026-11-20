"""Step 14: platform administration (only platform admins), suspend/reinstate with an audit trail,
and gateway hardening (security headers, body size limit)."""

import pytest
from conftest import ORIGIN, PASSWORD, bootstrap, login, shop
from fastapi.testclient import TestClient

PLATFORM = "http://platform.localhost:5173"


def platform_client(world):
    c = TestClient(
        world["gateway"],
        base_url=PLATFORM,
        raise_server_exceptions=False,
        headers={"Origin": PLATFORM},
    )
    csrf = bootstrap_at(c)
    r = c.post(
        "/api/v1/auth/login",
        json={"email": "pat@example.com", "password": PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    c.csrf = r.json()["csrf_token"]
    return c


def bootstrap_at(c):
    return c.get("/api/v1/auth/csrf").json()["csrf_token"]


@pytest.fixture
def pat(world):
    return platform_client(world)


def test_only_platform_admins(world):
    anon = TestClient(
        world["gateway"],
        base_url=PLATFORM,
        raise_server_exceptions=False,
        headers={"Origin": PLATFORM},
    )
    assert anon.get("/api/v1/platform/businesses").status_code == 401
    alice = world["client"]
    login(alice)  # owns a business, but is not a platform admin
    assert alice.get("/api/v1/platform/businesses").status_code == 403
    assert alice.get("/api/v1/platform/summary").status_code == 403


def test_summary_and_business_list(world, pat):
    s = pat.get("/api/v1/platform/summary").json()
    assert s["businesses"] == 4 and s["by_status"]["SUSPENDED"] == 1
    assert s["services"] == dict.fromkeys(
        ("identity", "store", "catalog", "supply", "sales", "billing", "agent"), "ready"
    )
    rows = {b["slug"]: b for b in pat.get("/api/v1/platform/businesses").json()["items"]}
    assert (
        set(rows) == {"acme", "beta", "gone", "legacy"}
        and rows["acme"]["owner_email"] == "alice@example.com"
    )
    assert rows["acme"]["members"] == 2 and rows["acme"]["customers"] == 1
    assert [
        b["slug"] for b in pat.get("/api/v1/platform/businesses?status=suspended").json()["items"]
    ] == ["gone"]


def test_suspend_closes_shop_and_admin_then_reinstate(world, pat):
    acme = world["ids"]["acme"]
    shopper = shop(world["gateway"])
    assert shopper.get("/api/v1/storefront").status_code == 200
    no_reason = pat.post(
        f"/api/v1/platform/businesses/{acme}/suspend",
        json={"reason": ""},
        headers={"X-CSRF-Token": pat.csrf},
    )
    assert no_reason.status_code == 422
    no_csrf = pat.post(
        f"/api/v1/platform/businesses/{acme}/suspend", json={"reason": "Fraud review"}
    )
    assert no_csrf.status_code == 403
    r = pat.post(
        f"/api/v1/platform/businesses/{acme}/suspend",
        json={"reason": "Fraud review"},
        headers={"X-CSRF-Token": pat.csrf},
    )
    assert r.status_code == 200 and r.json()["status"] == "SUSPENDED"
    assert (
        shopper.get("/api/v1/storefront").status_code == 404
    )  # immediately, not after a cache expires
    owner = world["client"]
    login(owner)
    assert (
        owner.get(f"/api/v1/admin/businesses/{acme}/orders").json()["error"]["code"]
        == "business_suspended"
    )
    events = pat.get(f"/api/v1/platform/audit?business_id={acme}").json()["items"]
    assert (
        events[0]["action"] == "business.suspended"
        and events[0]["after"]["reason"] == "Fraud review"
    )
    back = pat.post(
        f"/api/v1/platform/businesses/{acme}/reinstate", headers={"X-CSRF-Token": pat.csrf}
    ).json()
    assert back["status"] == "ACTIVE" and shopper.get("/api/v1/storefront").status_code == 200
    assert (
        pat.get(f"/api/v1/platform/audit?business_id={acme}").json()["items"][0]["action"]
        == "business.reinstated"
    )
    again = pat.post(
        f"/api/v1/platform/businesses/{acme}/reinstate", headers={"X-CSRF-Token": pat.csrf}
    )
    assert again.status_code == 200  # already active: nothing changes


def test_security_headers_and_body_limit(world):
    c = world["client"]
    r = c.get("/api/health")
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Content-Security-Policy",
        "Referrer-Policy",
    ):
        assert header in r.headers
    assert r.headers["X-Frame-Options"] == "DENY"
    huge = c.post(
        "/api/v1/auth/login",
        content=b"x" * (17 * 1024 * 1024),
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
    )
    assert huge.status_code == 413
    assert bootstrap(c)  # normal requests unaffected
