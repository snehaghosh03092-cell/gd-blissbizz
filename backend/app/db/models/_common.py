"""Shared column types and helpers for every service's model module.

Conventions (docs/03-database-architecture.md):
* ids are UUID strings (``String(36)``); append-only/high-volume tables use ``BIGPK``;
* money ``Numeric(12, 2)``, quantity ``Numeric(14, 3)``, unit cost ``Numeric(14, 4)``, rates
``Numeric(6, 3)``;
* timestamps are naive UTC;
* a tenant table carries ``business_id`` -> ``businesses.id`` (the only cross-service foreign key
allowed);
* enumerations are strings guarded by a named CHECK constraint (``enum_check``).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import mapped_column

MONEY = Numeric(12, 2)
QTY = Numeric(14, 3)
COST = Numeric(14, 4)
RATE = Numeric(6, 3)
# BIGINT auto-increment on MySQL, plain INTEGER on SQLite (which only autoincrements INTEGER PKs)
BIGPK = BigInteger().with_variant(Integer(), "sqlite")


def utc_now() -> datetime:
    # MySQL DATETIME has no timezone; consistently store naive UTC values.
    return datetime.now(UTC).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid4())


def id_column():
    return mapped_column(String(36), primary_key=True, default=new_id)


def business_fk(*, nullable: bool = False, primary_key: bool = False):
    """The tenant anchor: the one foreign key every service may point at."""
    return mapped_column(
        String(36),
        ForeignKey("businesses.id", ondelete="RESTRICT"),
        nullable=nullable,
        primary_key=primary_key,
    )


def enum_check(column: str, values: Sequence[str], name: str) -> CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({listed})", name=name)
