"""Verify empty-only demo loading, data integrity, credential rotation and hostname isolation."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import app.db.models  # noqa: F401
import pytest
from app.db.session import Base
from app.seed.reference import DEV_PASSWORD
from app.seed.world import build_world
from sqlalchemy import create_engine, text

spec = importlib.util.spec_from_file_location(
    "release_demo", Path(__file__).with_name("release-demo.py")
)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_only_exact_isolated_target_is_accepted():
    def target(name="blissbiz_next", host="db", user="blissbiz", dialect="mysql"):
        return SimpleNamespace(
            dialect=SimpleNamespace(name=dialect),
            url=SimpleNamespace(database=name, host=host, username=user),
        )

    demo.target_guard(target(), "blissbiz_next")
    for engine, confirmation in [
        (target(name="blissbiz"), "blissbiz_next"),
        (target(host="127.0.0.1"), "blissbiz_next"),
        (target(user="root"), "blissbiz_next"),
        (target(dialect="sqlite"), "blissbiz_next"),
        (target(), "yes"),
    ]:
        with pytest.raises(ValueError, match="Refusing target"):
            demo.target_guard(engine, confirmation)


def test_prepare_rotates_accounts_and_omits_operational_history(tmp_path):
    tmp_path.chmod(0o700)
    rows = build_world(datetime(2026, 11, 20), scale=0.01)
    before = {name: len(entries) for name, entries in rows.items()}
    rows = demo.prepare(rows, tmp_path, lambda p: "hash:" + p)
    credentials = json.loads((tmp_path / "credentials.json").read_text())
    assert (tmp_path / "credentials.json").stat().st_mode & 0o777 == 0o600
    assert len({c["password"] for c in credentials}) == len(credentials)
    assert all(
        c["password"] != DEV_PASSWORD and len(c["password"]) >= 40 for c in credentials
    )
    hashes = {
        c["email"]: "hash:" + c["password"]
        for c in credentials
        if c["kind"] != "shopper"
    }
    assert all(o["password_hash"] == hashes[o["email"]] for o in rows["owners"])
    assert all(c["password_hash"].startswith("hash:") for c in rows["customers"])
    assert not demo.EXCLUDED.intersection(rows)
    assert all(
        d["hostname"].endswith(".blissbiz.biz") and d["kind"] == "SUBDOMAIN"
        for d in rows["domains"]
    )
    assert len(rows["invoices"]) == before["invoices"]
    assert len(rows["inventory_transactions"]) == before["inventory_transactions"]
    with pytest.raises(FileExistsError):
        demo.prepare(rows, tmp_path, lambda p: "hash:" + p)


def test_non_private_credentials_directory_is_refused(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="0700"):
        demo.prepare({}, tmp_path, lambda p: p)
    assert not (tmp_path / "credentials.json").exists()


def test_import_passes_integrity_and_refuses_repeat(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    tmp_path.chmod(0o700)
    rows = demo.prepare(
        build_world(datetime(2026, 11, 20), scale=0.01), tmp_path, lambda p: "hash:" + p
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version(version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0016')"))
        demo.assert_empty(conn, Base.metadata)
        counts, checks = demo.insert_checked(conn, Base.metadata, rows)
        assert counts["businesses"] == 7 and counts["invoices"] > 0 and checks == 24
    with engine.begin() as conn:
        with pytest.raises(ValueError, match="non-empty"):
            demo.assert_empty(conn, Base.metadata)
        assert (
            conn.execute(text("SELECT COUNT(*) FROM owners")).scalar()
            == counts["owners"]
        )


def test_wrong_schema_is_refused():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version(version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('0004')"))
        with pytest.raises(ValueError, match="schema 0016"):
            demo.assert_empty(conn, Base.metadata)


def test_integrity_failure_rolls_back_all_inserted_rows(tmp_path):
    from decimal import Decimal

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    tmp_path.chmod(0o700)
    rows = demo.prepare(
        build_world(datetime(2026, 11, 20), scale=0.01), tmp_path, lambda p: "hash:" + p
    )
    rows["inventory_balances"][0]["on_hand"] += Decimal(1)
    with (
        pytest.raises(ValueError, match="integrity checks failed"),
        engine.begin() as conn,
    ):
        demo.insert_checked(conn, Base.metadata, rows)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM owners")).scalar() == 0
        assert (
            conn.execute(text("SELECT COUNT(*) FROM inventory_transactions")).scalar()
            == 0
        )
