"""Signed internal calls: only the gateway (holding the secret) may speak to a service."""

import json
import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext, decode_context, encode_context
from blissbiz_common.errors import ApiError, register_exception_handlers
from blissbiz_common.fastapi import make_internal_dependency
from blissbiz_common.logging import JsonFormatter, install_request_id
from blissbiz_common.signing import NonceCache, sign_request, verify_request

SECRET = "s" * 40
OTHER = "o" * 40
CTX = InternalContext(
    request_id="req-12345678",
    actor_type="OWNER",
    business_id="biz-1",
    actor_id="owner-1",
    role="MANAGER",
    permissions=("catalog.read", "catalog.write"),
)


def build_app() -> TestClient:
    settings = ServiceSettings(internal_signing_secret=SECRET, service_name="demo")
    internal = make_internal_dependency(settings)
    app = FastAPI()
    install_request_id(app)
    register_exception_handlers(app)

    @app.get("/internal/v1/businesses/{business_id}/ping")
    async def ping(business_id: str, ctx: InternalContext = Depends(internal)):
        ctx.require_business(business_id)
        return {"business": ctx.business_id, "actor": ctx.actor_id}

    @app.post("/internal/v1/things")
    async def create(payload: dict, ctx: InternalContext = Depends(internal)):
        ctx.require_permission("catalog.write")
        return {"echo": payload}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internals")

    return TestClient(app, raise_server_exceptions=False)


def call(client, method, path, body=b"", ctx=CTX, secret=SECRET, **kw):
    headers = sign_request(secret, method, path, body, ctx, **kw)
    headers["Content-Type"] = "application/json"  # not part of the signature
    return client.request(method, path, content=body, headers=headers)


# ---- context ---------------------------------------------------------------
def test_context_round_trips():
    assert decode_context(encode_context(CTX)) == CTX


def test_malformed_context_is_an_auth_failure_not_a_crash():
    with pytest.raises(ApiError) as e:
        decode_context("%%%not-base64%%%")
    assert e.value.status == 401


def test_selector_must_match_the_proven_business():
    assert CTX.require_business("biz-1") == "biz-1"
    with pytest.raises(ApiError) as e:
        CTX.require_business("biz-2")
    assert e.value.status == 404  # other tenants look like "not found", never "forbidden"
    with pytest.raises(ApiError):
        InternalContext("r" * 8, "ANONYMOUS").require_business("biz-1")


# ---- signing ---------------------------------------------------------------
def test_a_correctly_signed_request_is_accepted_and_yields_the_context():
    client = build_app()
    r = call(client, "GET", "/internal/v1/businesses/biz-1/ping")
    assert r.status_code == 200 and r.json() == {"business": "biz-1", "actor": "owner-1"}


def test_unsigned_requests_are_rejected():
    r = build_app().get("/internal/v1/businesses/biz-1/ping")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_internal_request"


def test_wrong_secret_is_rejected():
    assert (
        call(build_app(), "GET", "/internal/v1/businesses/biz-1/ping", secret=OTHER).status_code
        == 401
    )


def test_tampered_body_is_rejected():
    client = build_app()
    headers = sign_request(SECRET, "POST", "/internal/v1/things", b'{"a":1}', CTX)
    r = client.post("/internal/v1/things", content=b'{"a":2}', headers=headers)
    assert r.status_code == 401


def test_tampered_path_or_method_is_rejected():
    client = build_app()
    headers = sign_request(SECRET, "GET", "/internal/v1/businesses/biz-1/ping", b"", CTX)
    assert client.get("/internal/v1/businesses/biz-2/ping", headers=headers).status_code == 401
    assert client.post("/internal/v1/things", content=b"{}", headers=headers).status_code == 401


def test_tampered_context_cannot_escalate_to_another_business():
    client = build_app()
    headers = sign_request(SECRET, "GET", "/internal/v1/businesses/biz-2/ping", b"", CTX)
    forged = InternalContext(
        "req-12345678", "OWNER", business_id="biz-2", permissions=("catalog.write",)
    )
    headers["X-Internal-Context"] = encode_context(
        forged
    )  # attacker swaps the identity, keeps the signature
    assert client.get("/internal/v1/businesses/biz-2/ping", headers=headers).status_code == 401


def test_replayed_request_is_rejected():
    client = build_app()
    headers = sign_request(SECRET, "GET", "/internal/v1/businesses/biz-1/ping", b"", CTX)
    assert client.get("/internal/v1/businesses/biz-1/ping", headers=headers).status_code == 200
    assert client.get("/internal/v1/businesses/biz-1/ping", headers=headers).status_code == 401


@pytest.mark.parametrize("skew", [-120, 120])
def test_requests_outside_the_time_window_are_rejected(skew):
    nonces = NonceCache()
    headers = sign_request(SECRET, "GET", "/x", b"", CTX, now=1_000_000)
    with pytest.raises(ApiError):
        verify_request(SECRET, "GET", "/x", b"", headers, nonces, now=1_000_000 + skew)
    assert verify_request(SECRET, "GET", "/x", b"", headers, nonces, now=1_000_030) == CTX


def test_nonce_is_only_consumed_by_a_valid_signature():
    nonces = NonceCache()
    headers = sign_request(SECRET, "GET", "/x", b"", CTX, now=1_000_000)
    bad = dict(headers, **{"X-Internal-Signature": "0" * 64})
    with pytest.raises(ApiError):
        verify_request(SECRET, "GET", "/x", b"", bad, nonces, now=1_000_000)
    # the attacker's failed attempt must not burn the legitimate request's nonce
    assert verify_request(SECRET, "GET", "/x", b"", headers, nonces, now=1_000_000) == CTX


def test_unconfigured_or_short_secrets_are_refused():
    with pytest.raises(ValueError):
        sign_request("short", "GET", "/x", b"", CTX)
    with pytest.raises(ApiError) as e:
        verify_request("", "GET", "/x", b"", {}, NonceCache())
    assert e.value.status == 503
    with pytest.raises(ValueError):
        ServiceSettings(internal_signing_secret="too-short")


def test_permissions_are_enforced_from_the_signed_context_only():
    client = build_app()
    ok = call(client, "POST", "/internal/v1/things", b'{"x":1}')
    assert ok.status_code == 200
    reader = InternalContext("req-12345678", "OWNER", "biz-1", "o1", "STAFF", ("catalog.read",))
    denied = call(client, "POST", "/internal/v1/things", b'{"x":1}', ctx=reader)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "forbidden"


def test_cross_business_path_is_a_404_not_a_403():
    r = call(build_app(), "GET", "/internal/v1/businesses/biz-2/ping")
    assert r.status_code == 404


# ---- errors and request ids -------------------------------------------------
def test_unexpected_errors_never_leak_internals_and_carry_a_request_id():
    r = build_app().get("/boom", headers={"X-Request-ID": "trace-abcdef12"})
    assert r.status_code == 500
    assert "secret internals" not in r.text
    assert r.json()["error"] == {
        "code": "internal_error",
        "message": "Something went wrong.",
        "request_id": "trace-abcdef12",
    }
    assert r.headers["X-Request-ID"] == "trace-abcdef12"


def test_bad_incoming_request_ids_are_replaced():
    r = build_app().get("/boom", headers={"X-Request-ID": "x"})
    assert len(r.headers["X-Request-ID"]) == 32


def test_validation_errors_use_the_shared_envelope():
    client = build_app()
    r = call(client, "POST", "/internal/v1/things", b"not json")
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_logs_are_json_with_service_and_request_id():
    record = logging.LogRecord("demo", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.fields = {"business_id": "biz-1"}
    entry = json.loads(JsonFormatter("sales").format(record))
    assert entry["service"] == "sales" and entry["message"] == "hello world"
    assert entry["business_id"] == "biz-1" and entry["level"] == "INFO"
