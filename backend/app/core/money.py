"""Decimal helpers and the single rounding policy used everywhere money is computed.

Policy: amounts are rounded ROUND_HALF_UP to 2 decimal places per line; invoice totals are rounded
to the nearest
rupee with the difference shown as ``round_off``. Changing this requires a version bump and a
migration note.
"""

from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")


def D(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    return Decimal(str(value))


def round2(value) -> Decimal:
    return D(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def round_rupee(value) -> Decimal:
    return D(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
