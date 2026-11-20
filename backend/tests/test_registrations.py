from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.api.registrations import get_registration_limiter
from app.api.session_dependencies import get_session_service
from app.core.config import Settings, get_settings
from app.core.passwords import PasswordService
from app.db.models import Business, Owner, RateLimitBucket
from app.db.session import get_db
from app.db.sessions import SessionRepository
from app.main import app
from app.schemas.registrations import RegistrationRequest
from app.services.business_urls import BusinessUrlBuilder
from app.services.rate_limits import RegistrationRateLimiter
from app.services.sessions import SessionService


@pytest.fixture
def registration_settings():
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        auth_allowed_origins=["http://testserver"],
        admin_base_url="http://testserver",
        storefront_base_url="http://shops.example.com:5173",
    )


@pytest.fixture
def rate_store(tmp_path):
    # Separate committed rate transactions must survive main-request rollback.
    engine = create_engine(f"sqlite:///{tmp_path / 'rate-limits.sqlite'}")
    RateLimitBucket.__table__.create(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def registration_sessions(db, registration_settings):
    return SessionService(SessionRepository(db), registration_settings)


@pytest.fixture
def registration_limiter(rate_store, registration_settings):
    return RegistrationRateLimiter(rate_store, registration_settings, clock=lambda: 1800.0)


@pytest.fixture
def registration_client(
    db, registration_settings, registration_sessions, registration_limiter, monkeypatch
):
    for dependency, provider in {
        get_db: lambda: db,
        get_settings: lambda: registration_settings,
        get_session_service: lambda: registration_sessions,
        get_registration_limiter: lambda: registration_limiter,
    }.items():
        monkeypatch.setitem(app.dependency_overrides, dependency, provider)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def payload():
    unique = uuid4().hex
    return {
        "owner": {
            "full_name": " Test Owner ",
            "email": f"{unique}@example.com",
            "password": "a long test passphrase",
            "phone": "+919000000000",
        },
        "business": {"name": " Test Bakery ", "slug": f"shop-{unique}"},
    }


def csrf_headers(client):
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return {"Origin": "http://testserver", "X-CSRF-Token": response.json()["csrf_token"]}


def record_counts(db):
    return tuple(db.scalar(select(func.count()).select_from(model)) for model in (Owner, Business))


class TestRegistration:
    def test_optional_public_fields_are_explicitly_saved(self, registration_client, payload, db):
        payload["business"].update(
            description=" Fresh bread ",
            contact_email="HELLO@EXAMPLE.COM",
            contact_phone="+919000000001",
            address=" Shop address ",
            currency="INR",
        )
        response = registration_client.post(
            "/api/v1/registrations", json=payload, headers=csrf_headers(registration_client)
        )
        assert response.status_code == 201
        business = db.get(Business, response.json()["business"]["id"])
        assert business.description == "Fresh bread" and business.address == "Shop address"
        assert business.contact_email == "hello@example.com"
        assert business.contact_phone == "+919000000001"

    def test_creates_owner_business_and_rotated_session(
        self, registration_client, registration_sessions, registration_settings, payload, db
    ):
        payload["owner"]["email"] = payload["owner"]["email"].upper()
        payload["business"]["slug"] = payload["business"]["slug"].upper()
        headers = csrf_headers(registration_client)
        old_cookie = registration_client.cookies.get(registration_settings.session_cookie_name)
        response = registration_client.post("/api/v1/registrations", json=payload, headers=headers)
        assert response.status_code == 201, response.status_code
        result = response.json()
        owner = db.get(Owner, result["owner"]["id"])
        business = db.get(Business, result["business"]["id"])
        assert owner.email == payload["owner"]["email"].lower() and owner.full_name == "Test Owner"
        assert owner.password_hash.startswith("$argon2id$")
        assert PasswordService().verify(owner.password_hash, payload["owner"]["password"])
        assert business.owner_id == owner.id and business.currency == "INR"
        assert business.contact_email is None and business.contact_phone is None
        assert set(result["owner"]) == {"id", "full_name", "email"}
        assert set(result["business"]) == {"id", "name", "slug"}
        slug = business.slug
        assert result["urls"] == {
            "admin": f"http://testserver/businesses/{slug}",
            "storefront": f"http://{slug}.shops.example.com:5173",
        }
        new_cookie = registration_client.cookies.get(registration_settings.session_cookie_name)
        assert new_cookie != old_cookie and registration_sessions.current(old_cookie) is None
        assert registration_sessions.require_owner(new_cookie).owner_id == owner.id
        assert new_cookie not in response.text and payload["owner"]["password"] not in response.text
        assert (
            "password_hash" not in response.text and response.headers["cache-control"] == "no-store"
        )
        new_csrf = registration_client.get("/api/v1/auth/csrf").json()["csrf_token"]
        assert new_csrf != headers["X-CSRF-Token"]

    @pytest.mark.parametrize("kind", ["slug", "email", "reserved"])
    def test_conflicts_leave_no_partial_records(
        self, registration_client, payload, existing_business, db, kind
    ):
        if kind == "slug":
            payload["business"]["slug"] = existing_business.slug
        elif kind == "email":
            payload["owner"]["email"] = db.get(Owner, existing_business.owner_id).email.upper()
        else:
            payload["business"]["slug"] = "ADMIN"
        before = record_counts(db)
        headers = csrf_headers(registration_client)
        response = registration_client.post("/api/v1/registrations", json=payload, headers=headers)
        assert response.status_code == 409
        assert record_counts(db) == before and "set-cookie" not in response.headers
        assert response.json()["error"]["code"] in {"email_unavailable", "slug_unavailable"}

    @pytest.mark.parametrize(
        "section,field,value",
        [
            ("owner", "email", "bad-address"),
            ("owner", "email", "ツ@example.com"),
            ("owner", "password", "short"),
            ("owner", "password", "x" * 129),
            ("owner", "full_name", "   "),
            ("owner", "phone", "x" * 33),
            ("owner", "password_hash", "injected"),
            ("business", "owner_id", "other-owner"),
            ("business", "currency", "USD"),
            ("business", "slug", "bad_slug"),
            ("business", "description", "x" * 5001),
            ("business", "contact_email", "bad-address"),
        ],
    )
    def test_invalid_fields_return_safe_422(
        self, registration_client, payload, db, section, field, value
    ):
        payload[section][field] = value
        headers = csrf_headers(registration_client)
        before = record_counts(db)
        response = registration_client.post("/api/v1/registrations", json=payload, headers=headers)
        assert response.status_code == 422
        assert f"{section}.{field}" in response.json()["error"]["fields"]
        assert "a long test passphrase" not in response.text
        assert record_counts(db) == before

    @pytest.mark.parametrize("mode", ["missing-token", "no-cookie", "bad-origin"])
    def test_rejects_unverified_requests(self, registration_client, payload, db, mode):
        headers = csrf_headers(registration_client)
        if mode == "missing-token":
            headers.pop("X-CSRF-Token")
        elif mode == "no-cookie":
            registration_client.cookies.clear()
        else:
            headers["Origin"] = "http://evil.example.com"
        before = record_counts(db)
        response = registration_client.post("/api/v1/registrations", json=payload, headers=headers)
        assert response.status_code == 403 and record_counts(db) == before

    def test_existing_owner_session_cannot_register_another_account(
        self, registration_client, payload, db
    ):
        first = registration_client.post(
            "/api/v1/registrations", json=payload, headers=csrf_headers(registration_client)
        )
        assert first.status_code == 201
        before = record_counts(db)
        payload["owner"]["email"] = f"{uuid4().hex}@example.com"
        payload["business"]["slug"] = f"shop-{uuid4().hex}"
        second = registration_client.post(
            "/api/v1/registrations", json=payload, headers=csrf_headers(registration_client)
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "already_authenticated"
        assert record_counts(db) == before

    @pytest.mark.parametrize("failure", ["rotation", "commit"])
    def test_failures_restore_anonymous_session_and_roll_back_records(
        self,
        registration_client,
        registration_sessions,
        registration_settings,
        payload,
        db,
        monkeypatch,
        failure,
    ):
        headers = csrf_headers(registration_client)
        old_cookie = registration_client.cookies.get(registration_settings.session_cookie_name)
        before = record_counts(db)

        def fail(*args):
            raise OperationalError("private SQL", {}, Exception("private credentials"))

        if failure == "rotation":
            monkeypatch.setattr(registration_sessions, "rotate", fail)
        else:
            monkeypatch.setattr(db, "commit", fail)
        response = registration_client.post("/api/v1/registrations", json=payload, headers=headers)
        assert response.status_code == 503 and "private" not in response.text
        assert "set-cookie" not in response.headers and record_counts(db) == before
        assert registration_sessions.current(old_cookie) is not None

    def test_database_uniqueness_race_returns_409(
        self, registration_client, payload, existing_business, db, monkeypatch
    ):
        from app.db.businesses import BusinessRepository

        payload["business"]["slug"] = existing_business.slug
        monkeypatch.setattr(BusinessRepository, "slug_exists", lambda self, slug: False)
        before = record_counts(db)
        response = registration_client.post(
            "/api/v1/registrations", json=payload, headers=csrf_headers(registration_client)
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "registration_conflict"
        assert record_counts(db) == before

    def test_urls_ignore_forwarded_headers(self, registration_client, payload):
        headers = {**csrf_headers(registration_client), "X-Forwarded-Host": "evil.example"}
        response = registration_client.post("/api/v1/registrations", json=payload, headers=headers)
        assert response.status_code == 201
        assert "evil.example" not in str(response.json()["urls"])

    def test_rate_limit_survives_failed_registration_and_ignores_forwarded_ip(
        self, registration_client, registration_settings, payload
    ):
        registration_settings.registration_rate_limit = 1
        payload["business"]["slug"] = "admin"
        headers = csrf_headers(registration_client)
        first = registration_client.post("/api/v1/registrations", json=payload, headers=headers)
        assert first.status_code == 409
        response = registration_client.post(
            "/api/v1/registrations",
            json=payload,
            headers={**headers, "X-Forwarded-For": "198.51.100.1"},
        )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) == 900
        assert "set-cookie" not in response.headers


def test_password_hashes_are_salted_and_verification_is_safe():
    service = PasswordService()
    first = service.hash("a long test passphrase")
    second = service.hash("a long test passphrase")
    assert first != second and service.verify(first, "a long test passphrase")
    assert not service.verify(first, "wrong") and not service.verify("invalid", "wrong")


def test_password_is_not_trimmed_or_exposed_in_repr(payload):
    payload["owner"]["password"] = "  a long test passphrase  "
    model = RegistrationRequest.model_validate(payload)
    assert model.owner.password.get_secret_value() == payload["owner"]["password"]
    assert payload["owner"]["password"] not in repr(model)


@pytest.mark.parametrize(
    "field,value",
    [
        ("storefront_base_url", "http://127.0.0.1:5173"),
        ("storefront_base_url", "https://*.example.com"),
        ("admin_base_url", "https://admin.example.com/path"),
        ("registration_rate_limit", 0),
        ("registration_rate_window_seconds", 0),
    ],
)
def test_registration_config_rejects_invalid_values(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="sqlite://", **{field: value})


def test_production_urls_use_configured_https_domain():
    settings = Settings(
        _env_file=None,
        database_url="sqlite://",
        app_env="production",
        auth_allowed_origins=["https://admin.example.com"],
        storefront_base_url="https://example.com",
        admin_base_url="https://admin.example.com",
    )
    urls = BusinessUrlBuilder(settings).for_slug("happy-bakery")
    assert urls.storefront == "https://happy-bakery.example.com"
    assert urls.admin == "https://admin.example.com/businesses/happy-bakery"
