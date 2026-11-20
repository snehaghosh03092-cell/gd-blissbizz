from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.slugs import SlugPolicy
from app.db.models import Business, Owner
from app.db.session import get_db
from app.main import app


class TestSlugAvailability:
    def test_unused_slug_is_available(self, api_client):
        slug = f"free-{uuid4().hex}"
        response = api_client.get(f"/api/v1/business-slugs/{slug}/availability")
        assert response.status_code == 200
        assert response.json() == {"slug": slug, "available": True}

    def test_taken_slug_is_unavailable(self, api_client, existing_business):
        response = api_client.get(f"/api/v1/business-slugs/{existing_business.slug}/availability")
        assert response.status_code == 200
        assert response.json() == {"slug": existing_business.slug, "available": False}

    def test_mixed_case_is_normalized_before_lookup(self, api_client, existing_business):
        response = api_client.get(
            f"/api/v1/business-slugs/{existing_business.slug.upper()}/availability"
        )
        assert response.status_code == 200
        assert response.json() == {"slug": existing_business.slug, "available": False}

    @pytest.mark.parametrize("slug", ["admin", "API", "www", "mail", "assets"])
    def test_reserved_slug_is_unavailable(self, api_client, slug):
        response = api_client.get(f"/api/v1/business-slugs/{slug}/availability")
        assert response.status_code == 200
        assert response.json() == {"slug": slug.lower(), "available": False}

    @pytest.mark.parametrize(
        "slug", ["-cake", "cake-", "cake--shop", "cake_shop", "cake.shop", "café", "x" * 64, "   "]
    )
    def test_invalid_slug_has_field_error(self, api_client, slug):
        response = api_client.get(f"/api/v1/business-slugs/{slug}/availability")
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert "slug" in error["fields"]

    @pytest.mark.parametrize("slug", ["a", "123", "x" * 63])
    def test_valid_boundary_slugs(self, api_client, slug):
        response = api_client.get(f"/api/v1/business-slugs/{slug}/availability")
        assert response.status_code == 200
        assert response.json()["slug"] == slug


class TestDatabaseConstraints:
    def test_duplicate_slug_is_rejected(self, db, existing_business):
        db.add(
            Business(
                owner_id=existing_business.owner_id,
                name="Another shop",
                slug=existing_business.slug,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()

    def test_duplicate_owner_email_is_rejected(self, db, existing_business):
        owner = db.get(Owner, existing_business.owner_id)
        db.add(Owner(full_name="Another owner", email=owner.email, password_hash="test-only"))
        with pytest.raises(IntegrityError):
            db.flush()

    def test_business_requires_existing_owner(self, db):
        db.add(Business(owner_id=str(uuid4()), name="No owner", slug=f"shop-{uuid4().hex}"))
        with pytest.raises(IntegrityError):
            db.flush()


def test_database_error_does_not_expose_details(monkeypatch):
    # Force an outage deterministically without stopping the developer's database.
    class UnavailableDatabase:
        def scalar(self, statement):
            raise OperationalError("private SQL", {}, Exception("private connection credentials"))

    monkeypatch.setitem(app.dependency_overrides, get_db, lambda: UnavailableDatabase())
    with TestClient(app) as client:
        response = client.get("/api/v1/business-slugs/test-shop/availability")
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "database_unavailable", "message": "Database temporarily unavailable."}
    }
    assert "private" not in response.text


def test_reserved_slugs_do_not_need_database(monkeypatch):
    class UnavailableDatabase:
        def scalar(self, statement):
            raise AssertionError("Reserved names must not query the database")

    monkeypatch.setitem(app.dependency_overrides, get_db, lambda: UnavailableDatabase())
    with TestClient(app) as client:
        response = client.get("/api/v1/business-slugs/ADMIN/availability")
    assert response.status_code == 200
    assert response.json() == {"slug": "admin", "available": False}


def test_slug_policy_trims_and_normalizes():
    assert SlugPolicy.normalize(" Happy-Bakery ") == "happy-bakery"
