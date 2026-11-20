"""All seven services wired together in-process over ASGI on a throwaway SQLite database.

Owners use http://127.0.0.1:5173 (admin); shoppers use shop hostnames such as acme.localhost.
"""

import asyncio
import json as jsonlib
from datetime import datetime
from decimal import Decimal

import app.db.models  # noqa: F401
import httpx
import pytest
from agent_service.main import AgentSettings
from agent_service.main import create_app as create_agent
from app.core.passwords import PasswordService
from app.db.models import (
    Business,
    BusinessMember,
    BusinessTaxProfile,
    Customer,
    CustomerAddress,
    Owner,
)
from app.db.models.billing import TaxRateVersion
from app.db.models.catalog import Category, Product, ProductVariant
from app.db.models.store import Domain, PolicyDocument, Storefront, StorePolicy
from app.db.models.supply import InventoryBalance, InventoryTransaction, Warehouse
from app.db.session import Base, get_db
from app.seed.reference import tax_rate_rows
from billing_service.main import BillingSettings
from billing_service.main import create_app as create_billing
from blissbiz_common.context import InternalContext
from blissbiz_common.signing import sign_request
from catalog_service.main import CatalogSettings
from catalog_service.main import create_app as create_catalog
from fastapi.testclient import TestClient
from identity_service.main import IdentitySettings
from identity_service.main import create_app as create_identity
from sales_service.main import SalesSettings
from sales_service.main import create_app as create_sales
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from store_service.main import StoreSettings
from store_service.main import create_app as create_store
from supply_service.main import SupplySettings
from supply_service.main import create_app as create_supply

from gateway_service.main import GatewaySettings
from gateway_service.main import create_app as create_gateway

SECRET = "t" * 40
ORIGIN = "http://127.0.0.1:5173"
PASSWORD = "REDACTED-EXAMPLE-CREDENTIAL"
HASH = PasswordService().hash(PASSWORD)


def seed_stock(s: Session, acme: str, beta: str) -> dict:
    """Acme: 5 variants (healthy, low, draft, archived, out) in 2 warehouses. Beta: 1 variant."""

    def category(b, name, slug):
        c = Category(business_id=b, name=name, slug=slug)
        s.add(c)
        s.flush()
        return c

    def product(b, cat, name, status):
        p = Product(
            business_id=b,
            category_id=cat.id,
            name=name,
            slug=name.lower().replace(" ", "-"),
            status=status,
            brand="Threads",
            tax_class="GST_5",
            hsn_sac="6109",
        )
        s.add(p)
        s.flush()
        return p

    def variant(b, p, sku, threshold, cost=250, color=None):
        options = {"size": sku[-1], **({"color": color} if color else {})}
        v = ProductVariant(
            business_id=b,
            product_id=p.id,
            sku=sku,
            options=options,
            option_hash=f"{b}:{sku}",
            selling_price=Decimal("499"),
            mrp=Decimal("599"),
            cost_price=Decimal(cost),
            reorder_threshold=Decimal(threshold),
        )
        s.add(v)
        s.flush()
        return v

    def warehouse(b, code, default=False):
        w = Warehouse(business_id=b, code=code, name=f"{code} warehouse", is_default=default)
        s.add(w)
        s.flush()
        return w

    def stock(b, v, w, on_hand, reserved=0, cost="250"):
        s.add(
            InventoryBalance(
                business_id=b,
                variant_id=v.id,
                warehouse_id=w.id,
                on_hand=Decimal(on_hand),
                reserved=Decimal(reserved),
                avg_cost=Decimal(cost),
            )
        )
        if on_hand:
            s.add(
                InventoryTransaction(
                    business_id=b,
                    variant_id=v.id,
                    warehouse_id=w.id,
                    txn_type="OPENING",
                    quantity_delta=Decimal(on_hand),
                    unit_cost=Decimal(cost),
                    reason_code="OPENING_STOCK",
                    source_type="OPENING",
                    idempotency_key=f"open:{v.id}:{w.id}",
                    actor_type="SYSTEM",
                    created_at=datetime(2026, 1, 1),
                )
            )

    tops, hats = category(acme, "Tops", "tops"), category(acme, "Hats", "hats")
    main, store = warehouse(acme, "MAIN", True), warehouse(acme, "STORE")
    tee, hat = product(acme, tops, "Tee", "PUBLISHED"), product(acme, hats, "Hat", "DRAFT")
    old, scarf = (
        product(acme, tops, "Old Coat", "ARCHIVED"),
        product(acme, hats, "Scarf", "PUBLISHED"),
    )
    tee_s = variant(acme, tee, "TEE-S", 3, color="Black")
    tee_m = variant(acme, tee, "TEE-M", 3, color="White")
    variant(acme, hat, "HAT-1", 2)
    old_1, scarf_1 = variant(acme, old, "OLD-1", 2), variant(acme, scarf, "SCARF-1", 2)
    stock(acme, tee_s, main, 10, 2)
    stock(acme, tee_s, store, 4)
    stock(acme, tee_m, main, 2)  # available 2 <= threshold 3 -> LOW
    stock(acme, old_1, main, 5)  # archived: not for sale
    stock(acme, scarf_1, main, 0)  # published, nothing left -> OUT
    beta_cat = category(beta, "Gadgets", "gadgets")
    beta_main = warehouse(beta, "MAIN", True)
    widget = product(beta, beta_cat, "Beta Widget", "PUBLISHED")
    widget_1 = variant(beta, widget, "BETA-1", 2)
    stock(beta, widget_1, beta_main, 7)
    return {
        "tee": tee.id,
        "hat": hat.id,
        "old": old.id,
        "scarf": scarf.id,
        "tee_s": tee_s.id,
        "tee_m": tee_m.id,
        "scarf_1": scarf_1.id,
        "beta_product": widget.id,
        "beta_variant": widget_1.id,
    }


def seed_shops(s: Session, acme, beta, gone, legacy) -> dict:
    """Storefront side: hostnames, published shops, policies, GST profile, rates, customers."""
    acme.state_code, beta.state_code = "29", "27"  # Karnataka, Maharashtra
    s.add_all([TaxRateVersion(**row) for row in tax_rate_rows()])
    s.add(
        BusinessTaxProfile(
            business_id=acme.id,
            gst_registered=True,
            gstin="29ABCDE1234F1Z5",
            gstin_state_code="29",
            invoice_prefix="AC",
        )
    )
    s.add(BusinessTaxProfile(business_id=beta.id, gst_registered=False, prices_include_tax=True))
    hosts = [
        (acme, "acme.localhost", "ACTIVE"),
        (beta, "beta.localhost", "ACTIVE"),
        (gone, "gone.localhost", "ACTIVE"),
        (legacy, "legacy.localhost", "ACTIVE"),
        (acme, "old-acme.localhost", "REMOVED"),
    ]
    for b, host, status in hosts:
        s.add(Domain(business_id=b.id, hostname=host, status=status, provider="local"))
    config = {"hero": {"title": "Welcome"}, "sections": ["hero"]}
    for b, status in (
        (acme, "PUBLISHED"),
        (beta, "PUBLISHED"),
        (gone, "PUBLISHED"),
        (legacy, "DRAFT"),
    ):
        s.add(
            Storefront(
                business_id=b.id,
                template_key="fashion",
                status=status,
                theme={},
                draft_config=config,
                published_config=config if status == "PUBLISHED" else None,
            )
        )
    s.add(
        StorePolicy(
            business_id=acme.id,
            free_shipping_over=Decimal(999),
            flat_shipping_fee=Decimal(49),
            cancel_window_hours=24,
            return_window_days=7,
            show_exact_stock=False,
        )
    )
    s.add(StorePolicy(business_id=beta.id, show_exact_stock=True))
    s.add_all(
        [
            PolicyDocument(business_id=acme.id, slug="returns", title="Returns", body="7 days."),
            PolicyDocument(
                business_id=acme.id,
                slug="handbook",
                title="Handbook",
                audience="OWNER",
                body="Internal only.",
            ),
        ]
    )
    cara = Customer(
        business_id=acme.id,
        email="cara@example.com",
        password_hash=HASH,
        full_name="Cara Shopper",
        phone="9800000001",
    )
    bea = Customer(
        business_id=beta.id, email="cara@example.com", password_hash=HASH, full_name="Cara at Beta"
    )  # same email, different shop: a separate account
    s.add_all([cara, bea])
    s.flush()
    home = CustomerAddress(
        business_id=acme.id,
        customer_id=cara.id,
        recipient="Cara Shopper",
        phone="9800000001",
        line1="1 MG Road",
        city="Bengaluru",
        state_code="29",
        postal_code="560001",
        is_default=True,
    )
    away = CustomerAddress(
        business_id=acme.id,
        customer_id=cara.id,
        recipient="Cara Shopper",
        phone="9800000001",
        line1="2 FC Road",
        city="Pune",
        state_code="27",
        postal_code="411004",
    )
    s.add_all([home, away])
    s.flush()
    return {"cara": cara.id, "bea": bea.id, "home": home.id, "away": away.id}


def build_world(engine) -> dict:
    with Session(engine) as s:

        def owner(name, email, **kw):
            o = Owner(full_name=name, email=email, password_hash=HASH, **kw)
            s.add(o)
            s.flush()
            return o

        def business(owner_, name, slug, status="ACTIVE"):
            b = Business(owner_id=owner_.id, name=name, slug=slug, status=status)
            s.add(b)
            s.flush()
            return b

        alice, bob = owner("Alice", "alice@example.com"), owner("Bob", "bob@example.com")
        owner("Carol", "carol@example.com", disabled_at=datetime(2026, 1, 1))  # disabled
        dave = owner("Dave", "dave@example.com")
        eve = owner("Eve", "eve@example.com")
        owner("Pat Platform", "pat@example.com", platform_role="admin")  # platform administrator
        acme = business(alice, "Acme", "acme")
        beta = business(eve, "Beta", "beta")
        gone = business(eve, "Gone", "gone", status="SUSPENDED")
        s.add_all(
            [
                BusinessMember(business_id=acme.id, owner_id=alice.id, role="OWNER"),
                BusinessMember(business_id=acme.id, owner_id=bob.id, role="STAFF"),
                BusinessMember(business_id=beta.id, owner_id=alice.id, role="MANAGER"),
                BusinessMember(business_id=gone.id, owner_id=alice.id, role="MANAGER"),
            ]
        )
        legacy = business(dave, "Legacy Bakery", "legacy")  # registered via B02: no member row
        ids = {"acme": acme.id, "beta": beta.id, "gone": gone.id, "legacy": legacy.id}
        ids.update(seed_stock(s, acme.id, beta.id))
        ids.update(seed_shops(s, acme, beta, gone, legacy))
        s.commit()
        return ids


@pytest.fixture
def world(tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def fk(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    ids = build_world(engine)

    def per_request_session():
        with Session(engine) as session:
            yield session

    identity = create_identity(
        IdentitySettings(internal_signing_secret=SECRET, login_rate_limit=3),
        rate_limit_factory=sessionmaker(bind=engine),
    )
    catalog = create_catalog(CatalogSettings(internal_signing_secret=SECRET))
    supply = create_supply(
        SupplySettings(internal_signing_secret=SECRET, upload_dir=tmp_path / "uploads"),
        transports={"catalog": httpx.ASGITransport(app=catalog)},
    )
    store = create_store(StoreSettings(internal_signing_secret=SECRET))
    billing = create_billing(BillingSettings(internal_signing_secret=SECRET))

    def make_sales(**overrides):
        transports = {
            name: httpx.ASGITransport(app=svc)
            for name, svc in (
                ("identity", identity),
                ("store", store),
                ("catalog", catalog),
                ("supply", supply),
                ("billing", billing),
            )
        }
        transports.update(overrides)
        settings = SalesSettings(
            internal_signing_secret=SECRET, profile_cache_seconds=0, sweep_interval_seconds=0
        )  # sweeps are run explicitly in tests
        app_ = create_sales(
            settings, transports=transports, session_factory=sessionmaker(bind=engine)
        )
        app_.dependency_overrides[get_db] = per_request_session
        return app_

    sales = make_sales()

    def make_agent(claude_transport=None, claude_key=""):
        app_ = create_agent(
            AgentSettings(internal_signing_secret=SECRET, anthropic_api_key=claude_key),
            transports={
                "store": httpx.ASGITransport(app=store),
                "catalog": httpx.ASGITransport(app=catalog),
            },
            claude_transport=claude_transport,
        )
        app_.dependency_overrides[get_db] = per_request_session
        return app_

    agent = make_agent()
    for service in (identity, catalog, supply, store, billing):
        service.dependency_overrides[get_db] = per_request_session

    def make_gateway(**overrides):
        transports = {
            "identity_transport": httpx.ASGITransport(app=identity),
            "catalog_transport": httpx.ASGITransport(app=catalog),
            "supply_transport": httpx.ASGITransport(app=supply),
            "store_transport": httpx.ASGITransport(app=store),
            "sales_transport": httpx.ASGITransport(app=sales),
            "agent_transport": httpx.ASGITransport(app=agent),
            "billing_transport": httpx.ASGITransport(app=billing),
        }
        transports.update(overrides)
        settings = GatewaySettings(internal_signing_secret=SECRET, tenant_cache_seconds=0)
        return create_gateway(settings, **transports)

    gateway = make_gateway()
    client = TestClient(gateway, base_url="http://127.0.0.1:5173", raise_server_exceptions=False)
    yield {
        "client": client,
        "gateway": gateway,
        "identity": identity,
        "catalog": catalog,
        "supply": supply,
        "ids": ids,
        "engine": engine,
        "make_gateway": make_gateway,
        "store": store,
        "sales": sales,
        "billing": billing,
        "make_sales": make_sales,
        "agent": agent,
        "make_agent": make_agent,
    }
    engine.dispose()


def bootstrap(client) -> str:
    r = client.get("/api/v1/auth/csrf", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    return r.json()["csrf_token"]


def login(client, email="alice@example.com", password=PASSWORD, csrf=None, origin=ORIGIN):
    csrf = csrf or bootstrap(client)
    headers = {"X-CSRF-Token": csrf}
    if origin:
        headers["Origin"] = origin
    return client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, headers=headers
    )


def internal_call(app, method, path, ctx: InternalContext, json_body=None, secret=SECRET):
    """Call a service directly with a signed request (what the gateway does): (status, json)."""
    body = jsonlib.dumps(json_body).encode() if json_body is not None else b""

    async def go():
        headers = sign_request(secret, method, path, body, ctx)
        if body:
            headers["Content-Type"] = "application/json"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://svc") as c:
            r = await c.request(method, path, content=body or None, headers=headers)
            return r.status_code, r.json() if r.content else None

    return asyncio.run(go())


def shop(gateway, host="acme.localhost"):
    """A shopper's browser on one shop: the Host picks the shop, the Origin is that same shop."""
    return TestClient(
        gateway,
        base_url=f"http://{host}",
        raise_server_exceptions=False,
        headers={"Origin": f"http://{host}"},
    )


def shopper_csrf(client) -> str:
    r = client.get("/api/v1/storefront/auth/csrf")
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def shopper_login(client, email="cara@example.com", password=PASSWORD):
    csrf = shopper_csrf(client)
    r = client.post(
        "/api/v1/storefront/auth/login",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]
