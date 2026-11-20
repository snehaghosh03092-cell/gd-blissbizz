from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.auth import router
from app.api.errors import register_exception_handlers
from app.api.session_cookies import SessionCookies
from app.api.session_dependencies import (
    get_session_service,
    require_csrf_session,
    require_owner_mutation,
    require_owner_session,
)
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.db.models import BrowserSession
from app.db.session import get_db
from app.db.sessions import SessionRepository
from app.services.sessions import SessionService


@dataclass
class Clock:
    now: datetime = datetime(2030, 1, 1)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auth_settings():
    return Settings(
        _env_file=None, database_url="sqlite://", auth_allowed_origins=["http://testserver"]
    )


@pytest.fixture
def service(db, auth_settings, clock):
    return SessionService(SessionRepository(db), auth_settings, clock)


def make_auth_app(db, config, service):
    # Exercise real dependencies using test-only endpoints, never production debug routes.
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    register_exception_handlers(app)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: config
    app.dependency_overrides[get_session_service] = lambda: service

    @app.post("/api/v1/probe", dependencies=[Depends(require_csrf_session)])
    def mutate():
        return {"ok": True}

    @app.get("/api/v1/owner-probe", dependencies=[Depends(require_owner_session)])
    def owner_read():
        return {"ok": True}

    @app.post("/api/v1/owner-probe", dependencies=[Depends(require_owner_mutation)])
    def owner_write():
        return {"ok": True}

    return app


@pytest.fixture
def auth_client(db, auth_settings, service):
    with TestClient(make_auth_app(db, auth_settings, service)) as client:
        yield client


def bootstrap(client):
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def mutation_headers(csrf_token):
    return {"Origin": "http://testserver", "X-CSRF-Token": csrf_token}


class TestCsrfBootstrap:
    def test_creates_private_cookie_and_independent_csrf_token(
        self, auth_client, db, service, auth_settings
    ):
        response = auth_client.get("/api/v1/auth/csrf")
        assert response.status_code == 200
        assert set(response.json()) == {"csrf_token"}
        cookie = auth_client.cookies.get(auth_settings.session_cookie_name)
        csrf = response.json()["csrf_token"]
        record = db.get(BrowserSession, service.token_hash(cookie))
        assert record is not None and record.owner_id is None
        assert record.token_hash != cookie and record.csrf_token == csrf
        assert csrf != cookie and cookie not in response.text
        assert "httponly" in response.headers["set-cookie"].lower()
        assert "samesite=lax" in response.headers["set-cookie"].lower()
        assert "domain=" not in response.headers["set-cookie"].lower()
        assert "path=/" in response.headers["set-cookie"].lower()
        assert "secure" not in response.headers["set-cookie"].lower()
        assert response.headers["cache-control"] == "no-store"
        assert "Cookie" in response.headers["vary"]

    def test_reuses_session_without_extending_expiry(
        self, auth_client, service, clock, auth_settings
    ):
        csrf = bootstrap(auth_client)
        cookie = auth_client.cookies.get(auth_settings.session_cookie_name)
        expires = service.current(cookie).expires_at
        clock.now += timedelta(seconds=30)
        response = auth_client.get("/api/v1/auth/csrf")
        assert response.json()["csrf_token"] == csrf
        assert auth_client.cookies.get(auth_settings.session_cookie_name) == cookie
        assert service.current(cookie).expires_at == expires
        assert "Max-Age=870" in response.headers["set-cookie"]

    def test_expired_session_gets_fresh_anonymous_session(
        self, auth_client, service, clock, auth_settings
    ):
        old_csrf = bootstrap(auth_client)
        old_cookie = auth_client.cookies.get(auth_settings.session_cookie_name)
        clock.now += timedelta(seconds=auth_settings.anonymous_session_ttl_seconds)
        new_csrf = bootstrap(auth_client)
        assert old_csrf != new_csrf
        assert auth_client.cookies.get(auth_settings.session_cookie_name) != old_cookie
        assert service.current(old_cookie) is None

    @pytest.mark.parametrize("token", ["forged", "a" * 43, "x" * 4096])
    def test_never_adopts_unknown_cookie(self, auth_client, auth_settings, token):
        auth_client.cookies.set(auth_settings.session_cookie_name, token)
        response = auth_client.get("/api/v1/auth/csrf")
        assert response.status_code == 200
        assert f"{auth_settings.session_cookie_name}={token};" not in response.headers["set-cookie"]

    def test_non_ascii_cookie_is_rejected_by_service(self, service):
        assert service.current("é" * 43) is None


class TestCsrfValidation:
    def test_valid_session_token_and_origin_allow_mutation(self, auth_client):
        csrf = bootstrap(auth_client)
        response = auth_client.post("/api/v1/probe", headers=mutation_headers(csrf))
        assert response.status_code == 200

    @pytest.mark.parametrize("csrf", [None, "wrong", "x" * 43, "x" * 4096])
    def test_missing_or_wrong_csrf_is_rejected(self, auth_client, csrf):
        bootstrap(auth_client)
        headers = {"Origin": "http://testserver"}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        response = auth_client.post("/api/v1/probe", headers=headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_rejected"

    def test_csrf_without_session_is_rejected(self, auth_client):
        csrf = bootstrap(auth_client)
        auth_client.cookies.clear()
        assert auth_client.post("/api/v1/probe", headers=mutation_headers(csrf)).status_code == 403

    def test_token_from_another_session_is_rejected(self, auth_client):
        csrf = bootstrap(auth_client)
        auth_client.cookies.clear()
        bootstrap(auth_client)
        assert auth_client.post("/api/v1/probe", headers=mutation_headers(csrf)).status_code == 403

    def test_expired_session_cannot_mutate(self, auth_client, auth_settings, clock):
        csrf = bootstrap(auth_client)
        clock.now += timedelta(seconds=auth_settings.anonymous_session_ttl_seconds)
        assert auth_client.post("/api/v1/probe", headers=mutation_headers(csrf)).status_code == 403

    @pytest.mark.parametrize(
        "source", [None, "null", "https://evil.example", "http://testserver.evil.example"]
    )
    def test_missing_or_untrusted_source_is_rejected(self, auth_client, source):
        csrf = bootstrap(auth_client)
        headers = {"X-CSRF-Token": csrf}
        if source is not None:
            headers["Origin"] = source
        assert auth_client.post("/api/v1/probe", headers=headers).status_code == 403

    def test_referer_is_supported_when_origin_is_absent(self, auth_client):
        csrf = bootstrap(auth_client)
        response = auth_client.post(
            "/api/v1/probe", headers={"Referer": "http://testserver/form", "X-CSRF-Token": csrf}
        )
        assert response.status_code == 200

    def test_bad_origin_cannot_be_overridden_by_good_referer(self, auth_client):
        csrf = bootstrap(auth_client)
        response = auth_client.post(
            "/api/v1/probe",
            headers={
                **mutation_headers(csrf),
                "Origin": "null",
                "Referer": "http://testserver/form",
            },
        )
        assert response.status_code == 403

    def test_cross_site_fetch_metadata_is_rejected(self, auth_client):
        csrf = bootstrap(auth_client)
        response = auth_client.post(
            "/api/v1/probe", headers={**mutation_headers(csrf), "Sec-Fetch-Site": "cross-site"}
        )
        assert response.status_code == 403

    def test_storefront_host_and_forwarded_host_cannot_get_session(self, auth_client):
        response = auth_client.get(
            "/api/v1/auth/csrf",
            headers={"Host": "bakery.testserver", "X-Forwarded-Host": "testserver"},
        )
        assert response.status_code == 403 and "set-cookie" not in response.headers

    def test_untrusted_bootstrap_origin_is_rejected(self, auth_client):
        response = auth_client.get("/api/v1/auth/csrf", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403 and "set-cookie" not in response.headers


class TestSessionLifecycle:
    def test_rotation_invalidates_old_cookie_and_csrf(self, service, existing_business, clock):
        anonymous = service.bootstrap(None)
        old_csrf = anonymous.record.csrf_token
        authenticated = service.rotate(anonymous.raw_token, existing_business.owner_id)
        assert authenticated.raw_token != anonymous.raw_token
        assert authenticated.record.csrf_token != old_csrf
        assert authenticated.record.owner_id == existing_business.owner_id
        assert authenticated.record.expires_at == clock.now + timedelta(seconds=28800)
        assert service.current(anonymous.raw_token) is None
        assert service.require_owner(authenticated.raw_token).owner_id == existing_business.owner_id
        with pytest.raises(ApiError) as error:
            service.validate_csrf(authenticated.raw_token, old_csrf)
        assert error.value.status_code == 403

    def test_expired_session_cannot_rotate(self, service, clock):
        session = service.bootstrap(None)
        clock.now += timedelta(seconds=900)
        with pytest.raises(ApiError) as error:
            service.rotate(session.raw_token, None)
        assert error.value.status_code == 401

    def test_revoke_invalidates_cookie(self, service):
        session = service.bootstrap(None)
        service.revoke(session.raw_token)
        assert service.current(session.raw_token) is None

    def test_owner_session_is_not_downgraded_by_bootstrap(self, service, existing_business):
        anonymous = service.bootstrap(None)
        owner = service.rotate(anonymous.raw_token, existing_business.owner_id)
        resumed = service.bootstrap(owner.raw_token)
        assert resumed.raw_token == owner.raw_token
        assert resumed.record.owner_id == existing_business.owner_id

    def test_anonymous_and_expired_sessions_cannot_access_owner_routes(
        self, auth_client, service, auth_settings, existing_business, clock
    ):
        bootstrap(auth_client)
        assert auth_client.get("/api/v1/owner-probe").status_code == 401
        raw = auth_client.cookies.get(auth_settings.session_cookie_name)
        owner = service.rotate(raw, existing_business.owner_id)
        auth_client.cookies.clear()
        auth_client.cookies.set(auth_settings.session_cookie_name, owner.raw_token)
        assert auth_client.get("/api/v1/owner-probe").status_code == 200
        assert (
            auth_client.post(
                "/api/v1/owner-probe", headers={"Origin": "http://testserver"}
            ).status_code
            == 403
        )
        assert (
            auth_client.post(
                "/api/v1/owner-probe", headers=mutation_headers(owner.record.csrf_token)
            ).status_code
            == 200
        )
        clock.now += timedelta(seconds=auth_settings.owner_session_ttl_seconds)
        assert auth_client.get("/api/v1/owner-probe").status_code == 401

    def test_expired_cleanup_preserves_active_sessions(self, service, db, clock):
        expired = service.bootstrap(None)
        clock.now += timedelta(seconds=901)
        active = service.bootstrap(None)
        assert service.purge_expired() >= 1
        assert db.get(BrowserSession, expired.record.token_hash) is None
        assert service.current(active.raw_token) is not None

    def test_failed_rotation_can_roll_back_without_losing_session(self, service, db):
        from uuid import uuid4

        anonymous = service.bootstrap(None)
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                service.rotate(anonymous.raw_token, str(uuid4()))
        assert service.current(anonymous.raw_token) is not None
        assert service.current(anonymous.raw_token).owner_id is None

    def test_credentials_repr_hides_tokens(self, service):
        credentials = service.bootstrap(None)
        assert credentials.raw_token not in repr(credentials)
        assert credentials.record.csrf_token not in repr(credentials)


class TestProductionSettings:
    def test_production_refuses_plain_http_transport(self, db, clock):
        config = Settings(
            _env_file=None,
            database_url="sqlite://",
            app_env="production",
            auth_allowed_origins=["https://admin.example.com"],
            storefront_base_url="https://example.com",
            admin_base_url="https://admin.example.com",
        )
        service = SessionService(SessionRepository(db), config, clock)
        with TestClient(
            make_auth_app(db, config, service), base_url="http://admin.example.com"
        ) as client:
            response = client.get("/api/v1/auth/csrf")
        assert response.status_code == 403 and "set-cookie" not in response.headers

    def test_secure_host_only_cookie(self, db, clock):
        config = Settings(
            _env_file=None,
            database_url="sqlite://",
            app_env="production",
            auth_allowed_origins=["https://admin.example.com"],
            storefront_base_url="https://example.com",
            admin_base_url="https://admin.example.com",
        )
        service = SessionService(SessionRepository(db), config, clock)
        with TestClient(
            make_auth_app(db, config, service), base_url="https://admin.example.com"
        ) as client:
            response = client.get("/api/v1/auth/csrf")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-blissbiz_session=")
        assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie
        assert "Domain=" not in cookie and "SameSite=lax" in cookie

    @pytest.mark.parametrize(
        "origins", [[], ["*"], ["https://*.example.com"], ["https://admin.example.com/path"]]
    )
    def test_invalid_origin_configuration_is_rejected(self, origins):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, database_url="sqlite://", auth_allowed_origins=origins)

    def test_production_requires_explicit_https_origins(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, database_url="sqlite://", app_env="production")

    def test_origin_default_ports_are_normalized(self):
        config = Settings(
            _env_file=None,
            database_url="sqlite://",
            auth_allowed_origins=["https://ADMIN.example.com:443/"],
        )
        assert config.auth_allowed_origins == ["https://admin.example.com"]

    @pytest.mark.parametrize(
        "field", ["anonymous_session_ttl_seconds", "owner_session_ttl_seconds"]
    )
    def test_invalid_lifetimes_are_rejected(self, field):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, database_url="sqlite://", **{field: 0})


def test_database_failure_does_not_issue_cookie(auth_client, service, monkeypatch):
    def fail(record):
        raise OperationalError("secret SQL", {}, Exception("secret connection"))

    monkeypatch.setattr(service.repository, "add", fail)
    response = auth_client.get("/api/v1/auth/csrf")
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert "secret" not in response.text


def test_failed_commit_does_not_issue_cookie(auth_client, db, monkeypatch):
    def fail():
        raise OperationalError("secret SQL", {}, Exception("secret connection"))

    monkeypatch.setattr(db, "commit", fail)
    response = auth_client.get("/api/v1/auth/csrf")
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert "secret" not in response.text


def test_cookie_clear_uses_same_scope(auth_settings):
    response = Response()
    SessionCookies(auth_settings).clear(response)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("blissbiz_session=")
    assert "Max-Age=0" in cookie and "Path=/" in cookie and "HttpOnly" in cookie
    assert "Domain=" not in cookie
