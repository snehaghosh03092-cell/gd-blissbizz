"""Database triggers for tables that must never be edited: the stock ledger, audit events, issued
invoices.

The same SQL generator is used by ``Base.metadata.create_all`` (tests/SQLite, via an
``after_create`` listener)
and by the Alembic migration, so behaviour is identical everywhere. TRUNCATE does not fire DELETE
triggers in
MySQL, which keeps the dev-only ``seed --reset`` possible without weakening the rule for normal
operation.
"""

from sqlalchemy import event
from sqlalchemy.engine import Connection

from app.db.session import Base

APPEND_ONLY = (
    "inventory_transactions",
    "audit_events",
    "invoice_lines",
    "credit_notes",
    "credit_note_lines",
)
NO_DELETE = ("invoices", "payment_webhook_events")
# Columns of an issued invoice that may never change. status/pdf_* may (cancellation via credit
# note, PDF render).
INVOICE_IMMUTABLE = (
    "business_id",
    "order_id",
    "customer_id",
    "doc_type",
    "series",
    "fy",
    "number",
    "display_number",
    "issued_at",
    "seller",
    "buyer",
    "place_of_supply_state",
    "is_inter_state",
    "taxable_total",
    "cgst_total",
    "sgst_total",
    "igst_total",
    "cess_total",
    "round_off",
    "grand_total",
    "prices_include_tax",
    "tax_rate_version_ids",
    "content_hash",
)


def _abort(
    dialect: str, table: str, trigger: str, when: str, message: str, condition: str | None
) -> list[str]:
    drop = f"DROP TRIGGER IF EXISTS {trigger}"
    if dialect == "mysql":
        body = f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{message}'"
        if condition:
            body = f"IF {condition} THEN {body}; END IF"
        create = f"CREATE TRIGGER {trigger} {when} ON {table} FOR EACH ROW BEGIN {body}; END"
    else:  # sqlite
        guard = f" WHEN {condition}" if condition else ""
        create = (
            f"CREATE TRIGGER {trigger} {when} ON {table} FOR EACH ROW{guard} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
        )
    return [drop, create]


def trigger_statements(dialect: str) -> list[str]:
    if dialect not in {"mysql", "sqlite"}:
        return []
    changed = (
        (lambda c: f"NOT (OLD.{c} <=> NEW.{c})")
        if dialect == "mysql"
        else (lambda c: f"OLD.{c} IS NOT NEW.{c}")
    )
    statements: list[str] = []
    for table in APPEND_ONLY:
        for when, verb in (("BEFORE UPDATE", "no_update"), ("BEFORE DELETE", "no_delete")):
            statements += _abort(
                dialect, table, f"trg_{table}_{verb}", when, f"{table} is append-only", None
            )
    for table in NO_DELETE:
        statements += _abort(
            dialect,
            table,
            f"trg_{table}_no_delete",
            "BEFORE DELETE",
            f"{table} rows are never deleted",
            None,
        )
    statements += _abort(
        dialect,
        "invoices",
        "trg_invoices_immutable",
        "BEFORE UPDATE",
        "issued invoices are immutable; use a credit note",
        " OR ".join(changed(c) for c in INVOICE_IMMUTABLE),
    )
    return statements


def install_triggers(connection: Connection) -> None:
    for statement in trigger_statements(connection.dialect.name):
        connection.exec_driver_sql(statement)


def drop_triggers(connection: Connection) -> None:
    for statement in trigger_statements(connection.dialect.name):
        if statement.startswith("DROP TRIGGER"):
            connection.exec_driver_sql(statement)


@event.listens_for(Base.metadata, "after_create")
def _install_after_create(
    target, connection, **kwargs
) -> None:  # pragma: no cover - exercised by tests
    install_triggers(connection)
