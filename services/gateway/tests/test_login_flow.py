import httpx
from blissbiz_common.context import InternalContext
from blissbiz_common.signing import sign_request
from conftest import ORIGIN, PASSWORD, SECRET, bootstrap, login

COOKIE = "blissbiz_session"


def test_health_and_readiness(world):
    c = world["client"]
    assert c.get("/api/health").json() == {"status": "ok"}
    assert c.get("/api/ready").json() == {"status": "ready"}


def test_csrf_bootstrap_sets_a_hardened_cookie(world):
    r = world["client"].get("/api/v1/auth/csrf", headers={"Origin": ORIGIN})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert r.headers["cache-control"] == "no-store" and len(r.json()["csrf_token"]) >= 40
    assert r.headers["x-request-id"]


def test_login_succeeds_and_me_lists_memberships_with_roles(world):
    c = world["client"]
    r = login(c)
    assert r.status_code == 200
    body = r.json()
    assert body["owner"]["email"] == "alice@example.com" and "password" not in r.text
    assert {(b["slug"], b["role"]) for b in body["businesses"]} == {
        ("acme", "OWNER"), ("beta", "MANAGER"), ("gone", "MANAGER")}  # fmt: skip
    me = c.get("/api/v1/auth/me").json()
    assert me["owner"]["full_name"] == "Alice" and len(me["businesses"]) == 3


def test_registration_username_can_sign_in_and_is_unique(world):
    from fastapi.testclient import TestClient

    client = world["client"]
    csrf = bootstrap(client)
    payload = {
        "full_name": "Template Owner",
        "email": "template-owner@example.com",
        "username": "Template_Owner",
        "password": PASSWORD,
        "business_name": "Template Shop",
        "slug": "template-shop",
    }
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    endpoint = "/api/v1/auth/register"
    assert client.post(endpoint, json={**payload, "username": "bad name"}, headers=headers).status_code == 422
    registered = client.post(endpoint, json=payload, headers=headers)
    assert registered.status_code == 201
    with TestClient(world["gateway"], base_url=ORIGIN) as other:
        duplicate = other.post(
            endpoint,
            json={**payload, "email": "another-owner@example.com", "slug": "another-shop", "username": "template_owner"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": bootstrap(other)},
        )
        assert duplicate.status_code == 409
        result = login(other, email="TEMPLATE_OWNER")
        assert result.status_code == 200
        assert result.json()["owner"]["email"] == payload["email"]


def test_login_rotates_the_session_so_the_old_token_is_dead(world):
    c = world["client"]
    bootstrap(c)
    old = c.cookies.get(COOKIE)
    assert (
        login(
            c, csrf=c.get("/api/v1/auth/csrf", headers={"Origin": ORIGIN}).json()["csrf_token"]
        ).status_code
        == 200
    )
    assert c.cookies.get(COOKIE) != old
    # replaying the pre-login token from another client: it no longer resolves to anything
    from fastapi.testclient import TestClient

    attacker = TestClient(world["gateway"], base_url="http://127.0.0.1:5173")
    attacker.cookies.set(COOKIE, old)
    assert attacker.get("/api/v1/auth/me").status_code == 401


def test_wrong_password_and_unknown_email_look_identical(world):
    c = world["client"]
    a = login(c, password="nope-nope-nope-nope")
    b = login(world["client"], email="nobody@example.com")
    assert a.status_code == b.status_code == 401
    assert a.json()["error"]["code"] == b.json()["error"]["code"] == "invalid_credentials"
    assert a.json()["error"]["message"] == b.json()["error"]["message"]
    assert c.get("/api/v1/auth/me").status_code == 401  # still anonymous


def test_disabled_accounts_cannot_log_in(world):
    r = login(world["client"], email="carol@example.com")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


def test_repeated_failures_are_throttled_with_retry_after(world):
    c = world["client"]
    statuses = [login(c, password="wrong-wrong-wrong-1").status_code for _ in range(5)]
    assert statuses[:3] == [401, 401, 401] and statuses[3:] == [429, 429]
    blocked = login(c, password=PASSWORD)  # even the right password is refused while throttled
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0


def test_login_requires_csrf_token_and_a_trusted_origin(world):
    c = world["client"]
    csrf = bootstrap(c)
    body = {"email": "alice@example.com", "password": PASSWORD}
    assert c.post("/api/v1/auth/login", json=body, headers={"Origin": ORIGIN}).status_code == 403
    wrong_token = {"Origin": ORIGIN, "X-CSRF-Token": "x" * 43}
    assert c.post("/api/v1/auth/login", json=body, headers=wrong_token).status_code == 403
    assert login(c, csrf=csrf, origin="https://evil.example").status_code == 403
    assert (
        login(c, csrf=csrf, origin=None).status_code == 403
    )  # mutations need an Origin or Referer
    forbidden = c.post("/api/v1/auth/login", json=body, headers={
        "Origin": ORIGIN, "X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"})  # fmt: skip
    assert forbidden.status_code == 403
    assert login(c, csrf=csrf).status_code == 200  # the legitimate request still works


def test_logout_ends_the_session_everywhere(world):
    c = world["client"]
    csrf = login(c).json()["csrf_token"]
    token = c.cookies.get(COOKIE)
    assert (
        c.post("/api/v1/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
    )  # needs CSRF
    assert (
        c.post("/api/v1/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code
        == 204
    )
    from fastapi.testclient import TestClient

    replay = TestClient(world["gateway"], base_url="http://127.0.0.1:5173")
    replay.cookies.set(COOKIE, token)
    assert (
        replay.get("/api/v1/auth/me").status_code == 401
    )  # server-side revocation, not just cookie clearing


def test_businesses_registered_before_memberships_existed_still_belong_to_their_owner(world):
    c = world["client"]
    r = login(c, email="dave@example.com")
    assert [(b["slug"], b["role"]) for b in r.json()["businesses"]] == [("legacy", "OWNER")]


# ---- tenant context (what future admin routes rely on) --------------------------------
def test_membership_becomes_a_permissioned_context(world):
    c, ids = world["client"], world["ids"]
    login(c)
    owner = c.get(f"/api/v1/admin/businesses/{ids['acme']}").json()
    assert owner["role"] == "OWNER" and "team.manage" in owner["permissions"]
    manager = c.get(f"/api/v1/admin/businesses/{ids['beta']}").json()
    assert manager["role"] == "MANAGER" and "team.manage" not in manager["permissions"]
    assert "sales.refund" in manager["permissions"]


def test_staff_get_only_staff_permissions(world):
    c, ids = world["client"], world["ids"]
    login(c, email="bob@example.com")
    staff = c.get(f"/api/v1/admin/businesses/{ids['acme']}").json()
    assert staff["role"] == "STAFF" and "sales.refund" not in staff["permissions"]
    assert "catalog.read" in staff["permissions"] and "catalog.write" not in staff["permissions"]


def test_other_tenants_look_nonexistent_not_forbidden(world):
    c, ids = world["client"], world["ids"]
    login(c, email="bob@example.com")  # bob belongs only to acme
    assert c.get(f"/api/v1/admin/businesses/{ids['beta']}").status_code == 404
    assert c.get("/api/v1/admin/businesses/not-a-real-id").status_code == 404
    listed = c.get("/api/v1/admin/businesses").json()["items"]
    assert [b["slug"] for b in listed] == ["acme"]


def test_suspended_businesses_are_blocked(world):
    c, ids = world["client"], world["ids"]
    login(c)
    r = c.get(f"/api/v1/admin/businesses/{ids['gone']}")
    assert r.status_code == 403 and r.json()["error"]["code"] == "business_suspended"


def test_admin_routes_require_a_session(world):
    c, ids = world["client"], world["ids"]
    assert c.get(f"/api/v1/admin/businesses/{ids['acme']}").status_code == 401
    bootstrap(c)  # anonymous session is still not an owner
    assert c.get("/api/v1/admin/businesses").status_code == 401


# ---- service boundary ---------------------------------------------------------
def test_identity_refuses_direct_unsigned_calls(world):
    import asyncio

    transport = httpx.ASGITransport(app=world["identity"])

    async def attempt():
        async with httpx.AsyncClient(transport=transport, base_url="http://id") as client:
            unsigned = await client.post("/internal/v1/sessions/resolve", json={"raw_token": "x"})
            ctx = InternalContext("req-12345678", "SERVICE", actor_id="gateway")
            body = b'{"raw_token":"x"}'
            headers = sign_request("z" * 40, "POST", "/internal/v1/sessions/resolve", body, ctx)
            headers["Content-Type"] = "application/json"
            wrong = await client.post(
                "/internal/v1/sessions/resolve", content=body, headers=headers
            )
            return unsigned.status_code, wrong.status_code

    assert asyncio.run(attempt()) == (401, 401)


def test_a_down_identity_service_gives_a_clean_503(world):
    def refuse(request):
        raise httpx.ConnectError("refused")

    from fastapi.testclient import TestClient

    from gateway_service.main import GatewaySettings, create_app

    gateway = create_app(GatewaySettings(internal_signing_secret=SECRET),
                         identity_transport=httpx.MockTransport(refuse))  # fmt: skip
    client = TestClient(gateway, base_url="http://127.0.0.1:5173", raise_server_exceptions=False)
    ready = client.get("/api/ready")
    assert ready.status_code == 503 and ready.json()["error"]["code"] == "service_unavailable"
    assert client.get("/api/health").status_code == 200  # the gateway itself stays up


def test_errors_carry_the_request_id(world):
    r = world["client"].get("/api/v1/auth/me", headers={"X-Request-ID": "trace-0123456789"})
    assert r.status_code == 401 and r.json()["error"]["request_id"] == "trace-0123456789"
    assert r.headers["x-request-id"] == "trace-0123456789"
