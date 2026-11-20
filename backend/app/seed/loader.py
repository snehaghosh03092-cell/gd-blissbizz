"""Inserts a built world into a database (MySQL or SQLite) and resets it for development."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url

import app.db.models  # noqa: F401  (registers every table)
from app.db.session import Base
from app.db.triggers import drop_triggers, install_triggers

LOCAL_HOSTS = {None, "", "127.0.0.1", "localhost", "::1"}
KEEP = {"alembic_version"}


def assert_local(engine: Engine) -> None:
    url = make_url(str(engine.url)) if not hasattr(engine.url, "host") else engine.url
    if engine.dialect.name != "sqlite" and url.host not in LOCAL_HOSTS:
        raise SystemExit(
            f"Refusing to seed a non-local database ({url.host}). Seed data is for local development only."
        )


def reset(engine: Engine) -> None:
    """Empty every application table (dev only). TRUNCATE does not fire the append-only delete triggers."""
    assert_local(engine)
    with engine.begin() as conn:
        tables = [t.name for t in reversed(Base.metadata.sorted_tables) if t.name not in KEEP]
        if engine.dialect.name == "mysql":
            conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for name in tables:
                conn.execute(text(f"TRUNCATE TABLE `{name}`"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        else:  # SQLite: DELETE fires the append-only triggers, so lift them for the wipe only
            drop_triggers(conn)
            for name in tables:
                conn.execute(text(f'DELETE FROM "{name}"'))
            install_triggers(conn)


def is_empty(engine: Engine) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM owners")).scalar() == 0


def _clean(table, rows: list[dict]) -> list[dict]:
    cleaned = [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]
    keys: set[str] = set()
    for row in cleaned:
        keys |= row.keys()
    for row in cleaned:
        for key in keys - row.keys():
            column = table.c[key]
            if not column.nullable:
                raise ValueError(f"{table.name}.{key} missing in a row but NOT NULL")
            row[key] = None
    return cleaned


def load(engine: Engine, rows: dict[str, list[dict]]) -> dict[str, int]:
    assert_local(engine)
    counts: dict[str, int] = {}
    unknown = set(rows) - set(Base.metadata.tables)
    if unknown:
        raise ValueError(f"seed produced rows for unknown tables: {sorted(unknown)}")
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            table_rows = rows.get(table.name)
            if not table_rows:
                continue
            cleaned = _clean(table, table_rows)
            for start in range(0, len(cleaned), 2000):
                conn.execute(table.insert(), cleaned[start : start + 2000])
            counts[table.name] = len(cleaned)
    return counts
