#!/usr/bin/env python3
"""One-time synthetic demo import into the isolated VPS database; never resets data."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
from datetime import datetime
from pathlib import Path

# Deliberately omit operational history, queued deliveries, sessions and integration settings.
EXCLUDED = {
    "agent_conversations",
    "agent_messages",
    "audit_events",
    "error_events",
    "voice_logs",
    "onboarding_drafts",
    "onboarding_messages",
    "message_deliveries",
    "integration_configs",
    "payment_webhook_events",
}


def target_guard(engine, confirmation):
    if (
        confirmation != "blissbiz_next"
        or engine.dialect.name != "mysql"
        or engine.url.database != "blissbiz_next"
        or engine.url.host != "db"
        or engine.url.username != "blissbiz"
    ):
        raise ValueError(
            "Refusing target: requires isolated db/blissbiz_next as blissbiz"
        )


def assert_empty(conn, metadata):
    from sqlalchemy import select, text

    if conn.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalars().all() != ["0016"]:
        raise ValueError("Expected schema 0016; refusing import")
    for table in metadata.sorted_tables:
        if conn.execute(select(table).limit(1)).first() is not None:
            raise ValueError(f"Refusing non-empty application table: {table.name}")


def prepare(rows, credentials_dir, hash_password):
    """Returns only generated demo data and saves fresh credentials outside upload storage."""
    credentials_dir = Path(credentials_dir)
    info = credentials_dir.stat()
    if (
        not credentials_dir.is_dir()
        or credentials_dir.is_symlink()
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError(
            "Credential directory must be owned by runtime user and mode 0700"
        )
    credentials = []
    for name in EXCLUDED:
        rows.pop(name, None)
    # No known password remains on any account. Only one shopper per shop has a recorded login.
    unavailable_hash = hash_password(secrets.token_urlsafe(36))
    for customer in rows.get("customers", []):
        customer["password_hash"] = unavailable_hash
    by_business = {b["id"]: b["slug"] for b in rows["businesses"]}
    selected_shops = set()
    accounts = [("owner", row) for row in rows["owners"]]
    for row in rows.get("customers", []):
        if row["business_id"] not in selected_shops:
            accounts.append(("shopper", row))
            selected_shops.add(row["business_id"])
    for kind, account in accounts:
        password = secrets.token_urlsafe(30)
        account["password_hash"] = hash_password(password)
        credentials.append(
            {
                "kind": "platform_admin"
                if account.get("platform_role") == "admin"
                else kind,
                "email": account["email"],
                "password": password,
                "shop": by_business.get(account.get("business_id")),
            }
        )
    rows["domains"] = [d for d in rows.get("domains", []) if d["kind"] == "SUBDOMAIN"]
    for domain in rows["domains"]:
        domain["hostname"] = f"{by_business[domain['business_id']]}.blissbiz.biz"
        domain["provider"] = "shared_tenant"
        domain["verification"] = None
        domain["provider_ref"] = None
        # Certificate/host verification is performed separately, never claimed by demo data.
        domain["tls_status"] = "NONE"
    path = credentials_dir / "credentials.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(credentials, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return rows


def insert_checked(conn, metadata, rows):
    from app.seed.loader import _clean
    from app.seed.verify import CHECKS
    from sqlalchemy import text

    if set(rows) - set(metadata.tables):
        raise ValueError("Unknown tables in generated dataset")
    counts = {}
    for table in metadata.sorted_tables:
        entries = rows.get(table.name, [])
        if not entries:
            continue
        entries = _clean(table, entries)
        for start in range(0, len(entries), 1000):
            conn.execute(table.insert(), entries[start : start + 1000])
        counts[table.name] = len(entries)
    failures = {
        name: int(conn.execute(text(sql)).scalar() or 0) for name, sql in CHECKS.items()
    }
    if any(failures.values()):
        raise ValueError("Demo integrity checks failed; rolling back import")
    return counts, len(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-empty-demo", required=True)
    parser.add_argument("--credentials-dir", required=True)
    parser.add_argument(
        "--anchor", required=True, help="UTC timestamp without timezone"
    )
    args = parser.parse_args()
    anchor = datetime.fromisoformat(args.anchor)
    if anchor.tzinfo is not None:
        parser.error("anchor must be a naive UTC timestamp")

    import app.db.models  # noqa: F401
    from app.core.passwords import PasswordService
    from app.db.session import Base, engine
    from app.seed.world import build_world
    from sqlalchemy import text

    target_guard(engine, args.confirm_empty_demo)
    with engine.connect() as conn:
        if (
            conn.execute(
                text("SELECT GET_LOCK('blissbiz_next_demo_import', 0)")
            ).scalar()
            != 1
        ):
            raise ValueError("Another demo import holds the lock")
        conn.commit()
        try:
            with conn.begin():
                assert_empty(conn, Base.metadata)
                print(
                    "Empty schema verified. Generating synthetic demo data...",
                    flush=True,
                )
                rows = prepare(
                    build_world(anchor), args.credentials_dir, PasswordService().hash
                )
                counts, checks = insert_checked(conn, Base.metadata, rows)
            print(
                json.dumps(
                    {
                        "status": "imported",
                        "integrity_checks_passed": checks,
                        "rows": counts,
                    },
                    indent=2,
                ),
                flush=True,
            )
            print(
                "Private credentials saved in the mounted directory; do not paste them in chat."
            )
        finally:
            conn.execute(text("SELECT RELEASE_LOCK('blissbiz_next_demo_import')"))
            conn.commit()


if __name__ == "__main__":
    main()
