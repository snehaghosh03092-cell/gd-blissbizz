"""Generate docs/03a-schema-reference.md from the SQLAlchemy models (run: make schema-doc).

The hand-written design lives in docs/03-database-architecture.md; this file is the exact, always-current
column-level reference, so it is generated and must not be edited by hand.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app.db.models as models  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.db.views import VIEW_ORDER  # noqa: E402

ORDER = ["identity", "store", "catalog", "supply", "sales", "billing", "agent", "notification", "platform"]
TITLES = {
    "identity": "identity (accounts, tenants, sessions, onboarding)",
    "store": "store (storefront, policies, domains, deployments)",
    "catalog": "catalog (categories, products, variants, images, imports)",
    "supply": "supply (stock ledger, reservations, purchasing)",
    "sales": "sales (cart, orders, returns, payments)",
    "billing": "billing (tax rates, invoices, credit notes)",
    "agent": "agent (conversations, voice logs, recommendations)",
    "notification": "notification (in-app, templates, deliveries)",
    "platform": "shared platform tables (audit, idempotency, jobs, outbox, config)",
}


def col_type(column) -> str:
    return str(column.type).replace("DATETIME", "DATETIME")


def render() -> str:
    out = [
        "# 03a · Schema reference (generated)",
        "",
        "> Generated from the SQLAlchemy models by `make schema-doc`. **Do not edit by hand.** "
        "Design rationale is in [03-database-architecture.md](03-database-architecture.md).",
        "",
        f"{len(Base.metadata.tables)} tables, {len(VIEW_ORDER)} read views. "
        "Columns marked `FK` point at their parent table; `business_id` always references `businesses.id`.",
        "",
    ]
    by_owner: dict[str, list[str]] = {}
    for table, owner in models.OWNERS.items():
        by_owner.setdefault(owner, []).append(table)
    for owner in ORDER:
        tables = sorted(by_owner.get(owner, []))
        out += [f"## {TITLES[owner]}", "", f"Tables: {len(tables)}", ""]
        for name in tables:
            table = Base.metadata.tables[name]
            out += [f"### `{name}`", "", "| column | type | null | default / note |", "|---|---|---|---|"]
            fks = {fk.parent.name: f"FK → {fk.column.table.name}.{fk.column.name}" for fk in table.foreign_keys}
            for c in table.columns:
                notes = []
                if c.primary_key:
                    notes.append("PK")
                if c.name in fks:
                    notes.append(fks[c.name])
                if c.server_default is not None:
                    notes.append(f"server default {c.server_default.arg}")
                elif c.default is not None and getattr(c.default, "is_scalar", False):
                    notes.append(f"default {c.default.arg!r}")
                out.append(f"| `{c.name}` | {col_type(c)} | {'yes' if c.nullable else 'no'} | {'; '.join(notes)} |")
            rules = []
            for constraint in sorted(table.constraints, key=lambda x: str(x.name)):
                cname = constraint.name
                if constraint.__class__.__name__ == "UniqueConstraint":
                    rules.append(f"UNIQUE `{cname}` ({', '.join(c.name for c in constraint.columns)})")
                elif constraint.__class__.__name__ == "CheckConstraint":
                    rules.append(f"CHECK `{cname}`: `{constraint.sqltext}`")
            for index in sorted(table.indexes, key=lambda x: str(x.name)):
                rules.append(f"INDEX `{index.name}` ({', '.join(c.name for c in index.columns)})")
            if rules:
                out += ["", *[f"- {r}" for r in rules]]
            out.append("")
    out += ["## Read views (`rpt_*`)", "", "See `backend/app/db/views.py` for the SQL; every view exposes `business_id`.", ""]
    out += [f"- `{name}`" for name in VIEW_ORDER]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    target = ROOT / "docs" / "03a-schema-reference.md"
    target.write_text(render())
    print(f"wrote {target.relative_to(ROOT)} ({len(Base.metadata.tables)} tables)")
