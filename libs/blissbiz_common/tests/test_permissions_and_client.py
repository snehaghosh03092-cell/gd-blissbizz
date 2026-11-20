import httpx
import pytest
from fastapi import Depends, FastAPI

from blissbiz_common.clients import InternalClient
from blissbiz_common.config import ServiceSettings
from blissbiz_common.context import InternalContext
from blissbiz_common.errors import ApiError, register_exception_handlers
from blissbiz_common.fastapi import make_internal_dependency
from blissbiz_common.permissions import (
    MANAGER_PERMISSIONS,
    OWNER_PERMISSIONS,
    STAFF_PERMISSIONS,
    permissions_for,
)

SECRET = "k" * 40
CTX = InternalContext("req-12345678", "OWNER", "biz-1", "o1", "OWNER", ("catalog.read",))


def test_role_permissions_follow_the_matrix():
    assert STAFF_PERMISSIONS < MANAGER_PERMISSIONS < OWNER_PERMISSIONS
    assert "inventory.adjust.large" in OWNER_PERMISSIONS - MANAGER_PERMISSIONS
    assert "sales.refund" in MANAGER_PERMISSIONS and "sales.refund" not in STAFF_PERMISSIONS
    assert "team.manage" not in MANAGER_PERMISSIONS


def test_unknown_roles_get_no_permissions():
    assert permissions_for(None) == () and permissions_for("HACKER") == ()
    assert "catalog.write" in permissions_for("OWNER")


def real_app() -> FastAPI:
    internal = make_internal_dependency(ServiceSettings(internal_signing_secret=SECRET))
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/internal/v1/echo")
    async def echo(q: str = "", ctx: InternalContext = Depends(internal)):
        return {"q": q, "business": ctx.business_id, "rid": ctx.request_id}

    @app.post("/internal/v1/echo")
    async def post_echo(payload: dict, ctx: InternalContext = Depends(internal)):
        return payload

    return app


@pytest.mark.anyio
async def test_client_signs_get_with_query_and_post_with_body():
    client = InternalClient("http://svc", SECRET, transport=httpx.ASGITransport(app=real_app()))
    r = await client.request("GET", "/internal/v1/echo", CTX, params={"q": "a b&c"})
    assert r.status_code == 200 and r.json() == {
        "q": "a b&c",
        "business": "biz-1",
        "rid": "req-12345678",
    }
    r = await client.request("POST", "/internal/v1/echo", CTX, json_body={"x": [1, 2]})
    assert r.json() == {"x": [1, 2]}
    await client.aclose()


@pytest.mark.anyio
async def test_wrong_secret_is_rejected_by_the_service():
    client = InternalClient("http://svc", "w" * 40, transport=httpx.ASGITransport(app=real_app()))
    assert (await client.request("GET", "/internal/v1/echo", CTX)).status_code == 401


@pytest.mark.anyio
async def test_get_is_retried_but_post_is_not():
    calls = {"GET": 0, "POST": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls[request.method] += 1
        if calls[request.method] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = InternalClient("http://svc", SECRET, transport=httpx.MockTransport(flaky), backoff=0)
    assert (await client.request("GET", "/x", CTX)).status_code == 200 and calls["GET"] == 3
    assert (await client.request("POST", "/x", CTX, json_body={})).status_code == 503
    assert calls["POST"] == 1  # a non-idempotent call is never replayed


@pytest.mark.anyio
async def test_a_down_service_becomes_a_clean_503():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = InternalClient("http://svc", SECRET, transport=httpx.MockTransport(refuse), backoff=0)
    with pytest.raises(ApiError) as e:
        await client.request("GET", "/x", CTX)
    assert e.value.status == 503 and e.value.code == "service_unavailable"
