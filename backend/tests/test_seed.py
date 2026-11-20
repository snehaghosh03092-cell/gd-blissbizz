"""The development seed: integrity, determinism, scenario coverage, and safety. Uses a small scale
on SQLite."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

import app.db.models  # noqa: F401
from app.db.session import Base
from app.seed import loader
from app.seed.facts import collect
from app.seed.verify import CHECKS, run_checks, violations
from app.seed.world import build_world

ANCHOR = datetime(2026, 11, 18, 14, 0)


def sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def fk(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture(scope="module")
def seeded():
    engine = sqlite_engine()
    rows = build_world(ANCHOR, scale=0.35)
    counts = loader.load(engine, rows)
    yield engine, rows, counts
    engine.dispose()


def test_every_integrity_check_passes(seeded):
    engine, _, _ = seeded
    assert violations(engine) == {}
    assert len(run_checks(engine)) == len(CHECKS) >= 24


def test_all_seven_tenants_exist_in_their_states(seeded):
    engine, _, _ = seeded
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT slug, status, onboarding_state FROM businesses ORDER BY slug")
        ).all()
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        "freshbasket": ("ACTIVE", "ACTIVE"),
        "midnight-mart": ("SUSPENDED", "ACTIVE"),
        "newleaf": ("ONBOARDING", "READY_FOR_STORE_SETUP"),
        "pixelforge": ("ACTIVE", "ACTIVE"),
        "spiceroute": ("ACTIVE", "ACTIVE"),
        "threads-co": ("ACTIVE", "ACTIVE"),
        "voltedge": ("ACTIVE", "ACTIVE"),
    }


def test_data_supports_a_range_of_questions(seeded):
    """Each active business must contain the scenarios agents will be asked about."""
    engine, _, _ = seeded
    facts = {f["slug"]: f for f in collect(engine)}
    assert set(facts) == {"threads-co", "freshbasket", "voltedge", "pixelforge", "spiceroute"}
    for slug, f in facts.items():
        assert f["top_products_30d"], slug
        assert f["out_of_stock_published"], f"{slug}: no stock-out to ask about"
        assert f["dead_stock_60d"], f"{slug}: no dead stock to ask about"
        assert f["overdue_purchase_orders"], f"{slug}: no overdue purchase order"
        assert f["active_reservations"] > 0, slug
        assert f["order_status"].get("DELIVERED") and f["order_status"].get("CANCELLED"), slug
        assert set(f["payment_method"]) == {"ONLINE", "COD"}, slug
    assert any(f["credit_notes"] for f in facts.values())
    assert all(f["inter_state_invoices"] for f in facts.values())
    # both invoice kinds and both pricing modes are represented
    kinds = {k for f in facts.values() for k in f["invoices_by_type"]}
    assert kinds == {"TAX_INVOICE", "BILL_OF_SUPPLY"}
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT COUNT(DISTINCT prices_include_tax) FROM business_tax_profiles")
            ).scalar()
            == 2
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM products WHERE status='DRAFT'")).scalar() >= 1
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM products WHERE status='ARCHIVED'")).scalar()
            >= 1
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM stock_transfers WHERE status='IN_TRANSIT'")
            ).scalar()
            >= 1
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM purchase_invoices WHERE status='NEEDS_REVIEW'")
            ).scalar()
            >= 1
        )
        assert (
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM supplier_payments WHERE status<>'PAID' AND due_date < :d"
                ).bindparams(d="2026-11-18")
            ).scalar()
            >= 1
        )


def test_tax_rate_history_shows_effective_dating(seeded):
    engine, _, _ = seeded
    with engine.connect() as conn:
        retired = conn.execute(
            text("SELECT COUNT(*) FROM tax_rate_versions WHERE effective_to IS NOT NULL")
        ).scalar()
        current_classes = {
            r[0]
            for r in conn.execute(
                text("SELECT tax_class FROM tax_rate_versions WHERE effective_to IS NULL")
            )
        }
    assert retired == 2 and current_classes == {"GST_0", "GST_5", "GST_18", "GST_40"}


def test_nobody_in_the_seed_can_receive_real_messages(seeded):
    _, rows, _ = seeded
    emails = [r["email"] for r in rows["owners"]] + [r["email"] for r in rows["customers"]]
    assert all(e.endswith(".example") or e.endswith("@example.com") for e in emails)
    assert all(
        d["status"] != "SENT" for d in rows["message_deliveries"]
    )  # nothing claims to have been delivered


def test_seed_is_deterministic_for_the_same_inputs():
    a = build_world(ANCHOR, scale=0.12, seed=7)
    b = build_world(ANCHOR, scale=0.12, seed=7)
    c = build_world(ANCHOR, scale=0.12, seed=8)
    summary = lambda w: {t: len(r) for t, r in w.items()}  # noqa: E731
    assert summary(a) == summary(b)
    assert [r["sku"] for r in a["product_variants"]] == [r["sku"] for r in b["product_variants"]]
    assert [r["quantity_delta"] for r in a["inventory_transactions"]] == [
        r["quantity_delta"] for r in b["inventory_transactions"]
    ]
    assert summary(a) != summary(c)


def test_refuses_to_seed_a_non_local_database():
    remote = create_engine("mysql+pymysql://user:REDACTED-EXAMPLE-CREDENTIAL@db.prod.example/blissbiz")
    with pytest.raises(SystemExit):
        loader.assert_local(remote)
    with pytest.raises(SystemExit):
        loader.reset(remote)


def test_reset_empties_everything_and_allows_reseeding():
    engine = sqlite_engine()
    loader.load(engine, build_world(ANCHOR, scale=0.12))
    assert not loader.is_empty(engine)
    loader.reset(engine)
    assert loader.is_empty(engine)
    loader.load(engine, build_world(ANCHOR, scale=0.12))
    assert violations(engine) == {}
