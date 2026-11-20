"""Discount decisions execute only through catalog with tenant, price and retry checks."""

from decimal import Decimal

import pytest
from app.db.models.agent import Recommendation, RecommendationEvent
from app.db.models.catalog import ProductVariant
from blissbiz_common.errors import ApiError
from blissbiz_common.remote import SyncRemote
from conftest import ORIGIN, login, shop, shopper_login
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@pytest.fixture
def owner(world):
    client = world["client"]
    client.csrf = login(client).json()["csrf_token"]
    return client


def recommendation(world, **changes):
    values = dict(
        business_id=world["ids"]["acme"],
        variant_id=world["ids"]["tee_s"],
        rec_type="DISCOUNT",
        severity="medium",
        confidence="high",
        status="OPEN",
        dedup_key="DISCOUNT:test",
        title="Surplus Tee",
        summary="Surplus stock",
        action="10% off",
        suggested_discount_pct=Decimal(10),
        evidence={"unit_price": "499.00", "margin_floor_pct": 20},
    )
    values.update(changes)
    with Session(world["engine"]) as db:
        rec = Recommendation(**values)
        db.add(rec)
        db.commit()
        return rec.id


def accept(world, client, rid, action="accept", business="acme"):
    body = {}
    with Session(world["engine"]) as db:
        rec = db.get(Recommendation, rid)
        if action == "accept" and rec and rec.rec_type == "DISCOUNT" and rec.suggested_discount_pct:
            body = {
                "expected_price": rec.evidence.get("unit_price"),
                "discount_pct": str(rec.suggested_discount_pct),
            }
    return client.post(
        f"/api/v1/admin/businesses/{world['ids'][business]}/insights/recommendations/{rid}/{action}",
        json=body,
        headers={"X-CSRF-Token": client.csrf, "Origin": ORIGIN},
    )


def price(world, variant="tee_s"):
    with Session(world["engine"]) as db:
        return db.get(ProductVariant, world["ids"][variant]).selling_price


def test_discount_changes_storefront_and_checkout_once(world, owner):
    rid = recommendation(world)
    shopper = shop(world["gateway"])
    csrf = shopper_login(shopper)
    headers = {"X-CSRF-Token": csrf}
    added = shopper.post(
        "/api/v1/storefront/cart/items",
        json={"variant_id": world["ids"]["tee_s"], "quantity": "1"},
        headers=headers,
    )
    assert added.status_code == 200, added.text
    before = shopper.post("/api/v1/storefront/checkout/preview", json={}, headers=headers)
    assert before.json()["items"][0]["unit_price"] == "499.00"
    response = accept(world, owner, rid)
    assert response.status_code == 200, response.text
    assert response.json()["discount_application"]["selling_price"] == "449.10"
    assert response.json()["status"] == "ACCEPTED"
    assert price(world) == Decimal("449.10") and price(world, "tee_m") == Decimal("499.00")
    products = shopper.get("/api/v1/storefront/products").json()["items"]
    tee = next(p for p in products if p["name"] == "Tee")
    assert tee["price_min"] == "449.10"
    after = shopper.post("/api/v1/storefront/checkout/preview", json={}, headers=headers)
    assert after.status_code == 200, after.text
    assert after.json()["items"][0]["unit_price"] == "449.10"
    replay = accept(world, owner, rid)
    assert (
        replay.status_code == 200
        and replay.json()["discount_application"] == response.json()["discount_application"]
    )
    assert price(world) == Decimal("449.10")
    with Session(world["engine"]) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(RecommendationEvent)
                .where(RecommendationEvent.recommendation_id == rid)
            )
            == 1
        )


@pytest.mark.parametrize(
    "change,code",
    [("price", "price_changed"), ("cost", "margin_floor"), ("inactive", "unavailable_variant")],
)
def test_changed_prices_costs_or_availability_do_not_accept(world, owner, change, code):
    rid = recommendation(world)
    with Session(world["engine"]) as db:
        v = db.get(ProductVariant, world["ids"]["tee_s"])
        if change == "price":
            v.selling_price = Decimal("480")
        elif change == "cost":
            v.cost_price = Decimal("400")
        else:
            v.is_active = False
        db.commit()
    response = accept(world, owner, rid)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == code
    with Session(world["engine"]) as db:
        assert db.get(Recommendation, rid).status == "OPEN"
        assert not (db.get(Recommendation, rid).evidence or {}).get("discount_application")


def test_catalog_success_lost_response_can_retry_without_compounding(world, owner, monkeypatch):
    rid = recommendation(world)
    original = SyncRemote.call
    failed = False

    def lose_response(self, service, method, path, ctx, **kwargs):
        nonlocal failed
        result = original(self, service, method, path, ctx, **kwargs)
        if service == "catalog" and path.endswith("/recommendation-discount") and not failed:
            failed = True
            raise ApiError(503, "service_unavailable", "Lost response")
        return result

    monkeypatch.setattr(SyncRemote, "call", lose_response)
    assert accept(world, owner, rid).status_code == 503
    assert price(world) == Decimal("449.10")
    with Session(world["engine"]) as db:
        assert db.get(Recommendation, rid).status == "OPEN"
    assert accept(world, owner, rid).status_code == 200
    assert price(world) == Decimal("449.10")


def test_cross_business_recommendation_and_variant_cannot_change_prices(world, owner):
    rid = recommendation(world)
    assert accept(world, owner, rid, business="beta").status_code == 404
    foreign = recommendation(
        world, dedup_key="DISCOUNT:foreign", variant_id=world["ids"]["beta_variant"]
    )
    assert accept(world, owner, foreign).status_code == 404
    assert price(world) == price(world, "beta_variant") == Decimal("499.00")


def test_discount_requires_both_permissions_and_csrf(world, owner):
    rid = recommendation(world)
    url = f"/api/v1/admin/businesses/{world['ids']['acme']}/insights/recommendations/{rid}/accept"
    assert owner.post(url, json={}, headers={"Origin": ORIGIN}).status_code == 403
    staff = TestClient(world["gateway"], base_url=ORIGIN)
    staff.csrf = login(staff, email="bob@example.com").json()["csrf_token"]
    assert accept(world, staff, rid).status_code == 403
    assert price(world) == Decimal("499.00")


@pytest.mark.parametrize("rec_type", ["REORDER", "DEAD_STOCK", "REVIEW"])
def test_other_types_stay_decision_only(world, owner, rec_type):
    rid = recommendation(world, rec_type=rec_type)
    result = accept(world, owner, rid)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "ACCEPTED"
    assert result.json()["discount_application"] is None
    assert price(world) == Decimal("499.00")


def test_previously_accepted_discount_can_apply_once(world, owner):
    rid = recommendation(world, status="ACCEPTED")
    assert accept(world, owner, rid).status_code == 200
    assert price(world) == Decimal("449.10")


def test_dismissed_or_missing_discount_never_changes_price(world, owner):
    rid = recommendation(world, status="DISMISSED")
    assert accept(world, owner, rid).status_code == 409
    empty = recommendation(world, dedup_key="DISCOUNT:no-discount", suggested_discount_pct=None)
    assert accept(world, owner, empty).status_code == 409
    assert price(world) == Decimal("499.00")


def test_insights_permission_alone_cannot_apply_discount(world):
    from blissbiz_common.context import InternalContext
    from conftest import internal_call

    rid = recommendation(world)
    context = InternalContext(
        request_id="discount-test",
        actor_type="OWNER",
        business_id=world["ids"]["acme"],
        actor_id="test-owner",
        permissions=("insights.act",),
    )
    response = internal_call(
        world["agent"], "POST", f"/internal/v1/insights/recommendations/{rid}/accept", context, {}
    )
    assert response[0] == 403
    assert price(world) == Decimal("499.00")


def test_changed_suggestion_requires_new_confirmation(world, owner):
    rid = recommendation(world)
    url = f"/api/v1/admin/businesses/{world['ids']['acme']}/insights/recommendations/{rid}/accept"
    with Session(world["engine"]) as db:
        db.get(Recommendation, rid).suggested_discount_pct = Decimal(15)
        db.commit()
    result = owner.post(
        url,
        json={"expected_price": "499.00", "discount_pct": "10"},
        headers={"X-CSRF-Token": owner.csrf, "Origin": ORIGIN},
    )
    assert result.status_code == 409 and result.json()["error"]["code"] == "recommendation_changed"
    assert price(world) == Decimal("499.00")
    result = owner.post(url, json={}, headers={"X-CSRF-Token": owner.csrf, "Origin": ORIGIN})
    assert result.status_code == 409


def test_legacy_accepted_discount_is_listed_until_applied(world, owner):
    rid = recommendation(world, status="ACCEPTED")
    url = f"/api/v1/admin/businesses/{world['ids']['acme']}/insights/recommendations"
    before = owner.get(url).json()["items"]
    assert rid in [r["id"] for r in before]
    assert accept(world, owner, rid).status_code == 200
    after = owner.get(url).json()["items"]
    assert rid not in [r["id"] for r in after]


def test_discount_keeps_existing_order_prices(world, owner):
    from app.db.models.sales import OrderItem

    rid = recommendation(world)
    shopper = shop(world["gateway"])
    csrf = shopper_login(shopper)
    headers = {"X-CSRF-Token": csrf}
    assert (
        shopper.post(
            "/api/v1/storefront/cart/items",
            json={"variant_id": world["ids"]["tee_s"], "quantity": 1},
            headers=headers,
        ).status_code
        == 200
    )
    placed = shopper.post(
        "/api/v1/storefront/checkout",
        json={"address_id": world["ids"]["home"]},
        headers={**headers, "Idempotency-Key": "discount-old-order-1"},
    )
    assert placed.status_code == 201, placed.text
    with Session(world["engine"]) as db:
        old_line = db.scalar(select(OrderItem).where(OrderItem.variant_id == world["ids"]["tee_s"]))
        line_id, old_price, old_total = old_line.id, old_line.unit_price, old_line.line_total
    assert accept(world, owner, rid).status_code == 200
    with Session(world["engine"]) as db:
        line = db.get(OrderItem, line_id)
        assert line.unit_price == old_price == Decimal("499.00")
        assert line.line_total == old_total


def test_target_price_without_receipt_is_not_reported_as_new_discount(world, owner):
    """A manually changed price matching the target is not proof that we applied this discount."""
    rid = recommendation(world, suggested_discount_pct=Decimal(30))
    with Session(world["engine"]) as db:
        variant = db.get(ProductVariant, world["ids"]["tee_s"])
        variant.selling_price = Decimal("349.30")
        original_mrp = variant.mrp
        db.commit()
    response = accept(world, owner, rid)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "price_changed"
    with Session(world["engine"]) as db:
        variant = db.get(ProductVariant, world["ids"]["tee_s"])
        assert variant.selling_price == Decimal("349.30")
        assert variant.mrp == original_mrp
        rec = db.get(Recommendation, rid)
        assert rec.status == "OPEN" and not rec.evidence.get("discount_application")


def test_thirty_percent_discount_reduces_selling_price_without_inflating_mrp(world, owner):
    from app.db.models.platform import IdempotencyKey

    rid = recommendation(world, suggested_discount_pct=Decimal(30))
    with Session(world["engine"]) as db:
        original_mrp = db.get(ProductVariant, world["ids"]["tee_s"]).mrp
    response = accept(world, owner, rid)
    assert response.status_code == 200, response.text
    application = response.json()["discount_application"]
    assert Decimal(application["original_price"]) == Decimal("499.00")
    assert Decimal(application["selling_price"]) == Decimal("349.30")
    with Session(world["engine"]) as db:
        variant = db.get(ProductVariant, world["ids"]["tee_s"])
        assert variant.selling_price == Decimal("349.30")
        assert variant.mrp == original_mrp == Decimal("599.00")
        receipt = db.scalar(
            select(IdempotencyKey).where(IdempotencyKey.scope == "catalog.recommendation-discount")
        )
        assert receipt.service == "catalog" and receipt.response_body == application


def test_discount_receipt_rejects_changed_payload(world, owner):
    from blissbiz_common.context import InternalContext
    from conftest import internal_call

    rid = recommendation(world)
    assert accept(world, owner, rid).status_code == 200
    ctx = InternalContext(
        request_id="receipt-conflict",
        actor_type="OWNER",
        business_id=world["ids"]["acme"],
        permissions=("catalog.write", "insights.act"),
    )
    response = internal_call(
        world["catalog"],
        "POST",
        f"/internal/v1/variants/{world['ids']['tee_s']}/recommendation-discount",
        ctx,
        {"application_key": f"{rid}:legacy", "original_price": "499", "discount_pct": "20"},
    )
    assert response[0] == 409 and response[1]["error"]["code"] == "discount_conflict"
    assert price(world) == Decimal("449.10")


def test_recommendation_exposes_the_exact_size_colour_for_storefront_navigation(world, owner):
    rid = recommendation(world, status="ACCEPTED")
    base = f"/api/v1/admin/businesses/{world['ids']['acme']}/insights/recommendations"
    items = owner.get(base).json()["items"]
    rec = next(r for r in items if r["id"] == rid)
    assert rec["variant"]["variant_id"] == world["ids"]["tee_s"]
    assert rec["variant"]["product_slug"] == "tee"
    assert rec["variant"]["options"]["size"] == "S"
    assert rec["variant"]["sku"] == "TEE-S"
    # The distinct variant remains full price, so a generic first-option view can differ.
    assert accept(world, owner, rid).status_code == 200
    assert price(world, "tee_m") == Decimal("499.00")
    assert price(world, "tee_s") == Decimal("449.10")
    beta = owner.get(base.replace(world["ids"]["acme"], world["ids"]["beta"])).json()["items"]
    assert all(r.get("variant_id") != world["ids"]["tee_s"] for r in beta)
