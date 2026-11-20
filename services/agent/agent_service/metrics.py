"""Pure, deterministic inventory metrics (no I/O). All arithmetic uses Decimal.

Ported from the test1 Inventory Intelligence Agent (app/analytics/metrics.py). Lot/expiry (FEFO)
helpers were left out because this schema has no lots or expiry dates.

Conventions
-----------
* Division by zero never yields infinity: functions return ``None`` ("not applicable").
* Quantities use 3 decimal places, money 2, ratios 4 (see ``q3``/``q2``/``q4``).
* A velocity window covers *complete* days that end the day before ``as_of``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

CALCULATION_VERSION = "1.0"
MIN_HISTORY_DAYS = 14  # below this, demand estimates are low-confidence
SURPLUS_COVER_MULTIPLIER = Decimal("1.5")  # cover must exceed target by 50% to flag surplus
MIN_DISCOUNT_PCT = Decimal("5")  # smaller discounts are not worth recommending


def D(value) -> Decimal:
    """Coerce to Decimal without float artefacts."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    return Decimal(str(value))


def q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def q3(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def q4(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def num(x: Decimal | None, places: int = 2) -> float | None:
    """JSON-friendly rounded float (None preserved)."""
    if x is None:
        return None
    return float(round(D(x), places))


# ---------------------------------------------------------------- demand ----


def average_daily_sales(net_units: Decimal, eligible_days: int) -> Decimal | None:
    """Net units per eligible day; negative net (returns > sales) is clamped to 0.

    ``None`` when there are no eligible days (product has no history in the window).
    """
    if eligible_days <= 0:
        return None
    net = max(D(net_units), ZERO)
    return net / Decimal(eligible_days)


def trend_pct(current: Decimal | None, prior: Decimal | None) -> Decimal | None:
    """Relative change in velocity, e.g. -0.5 for a 50% drop. None without a prior baseline."""
    if current is None or prior is None or prior <= ZERO:
        return None
    return (current - prior) / prior


def eligible_days_in_window(first_received: date | None, window_start: date, window_end: date) -> int:
    """Days in [window_start, window_end] on/after the product was first stocked."""
    if first_received is None or window_end < window_start:
        return 0
    start = max(window_start, first_received)
    if start > window_end:
        return 0
    return (window_end - start).days + 1


# ----------------------------------------------------------- availability ---


def available_qty(on_hand: Decimal, reserved: Decimal) -> Decimal:
    return max(D(on_hand) - D(reserved), ZERO)


def days_of_cover(available: Decimal, velocity: Decimal | None) -> Decimal | None:
    if velocity is None or velocity <= ZERO:
        return None
    return D(available) / velocity


def projected_stockout_date(as_of: date, cover_days: Decimal | None) -> date | None:
    if cover_days is None:
        return None
    return as_of + timedelta(days=int(cover_days.to_integral_value(rounding=ROUND_DOWN)))


# ---------------------------------------------------------- replenishment ---


def reorder_point(velocity: Decimal, lead_time_days: int, safety_stock: Decimal) -> Decimal:
    return velocity * Decimal(max(lead_time_days, 0)) + D(safety_stock)


def target_stock(velocity: Decimal, target_cover_days: int, safety_stock: Decimal) -> Decimal:
    return velocity * Decimal(max(target_cover_days, 0)) + D(safety_stock)


def _round_up_to_multiple(qty: Decimal, multiple: int) -> Decimal:
    m = Decimal(max(multiple, 1))
    return (qty / m).to_integral_value(rounding=ROUND_CEILING) * m


def suggested_reorder_qty(
    target: Decimal,
    available: Decimal,
    incoming: Decimal,
    min_order_qty: Decimal,
    pack_size: int,
) -> Decimal:
    """Quantity to order: cover the gap to target, honour pack size and minimum order."""
    need = D(target) - D(available) - D(incoming)
    if need <= ZERO:
        return ZERO
    qty = _round_up_to_multiple(need, pack_size)
    minimum = D(min_order_qty)
    if qty < minimum:
        qty = _round_up_to_multiple(minimum, pack_size)
    return qty


def excess_qty(available: Decimal, incoming: Decimal, target: Decimal) -> Decimal:
    return max(D(available) + D(incoming) - D(target), ZERO)


# ---------------------------------------------------------------- pricing ---


def margin_pct(price: Decimal, cost: Decimal) -> Decimal | None:
    """Gross margin as a fraction of price; None when price is not positive."""
    price, cost = D(price), D(cost)
    if price <= ZERO:
        return None
    return (price - cost) / price


def max_discount_fraction(price: Decimal, cost: Decimal, margin_floor: Decimal) -> Decimal:
    """Largest discount fraction keeping margin >= margin_floor (0 when none is possible).

    margin after discount d: (p(1-d) - c) / (p(1-d)) >= f  <=>  d <= 1 - c / ((1-f) p)
    """
    price, cost, floor = D(price), D(cost), D(margin_floor)
    if price <= ZERO or floor >= ONE:
        return ZERO
    limit = ONE - cost / ((ONE - floor) * price)
    return min(max(limit, ZERO), ONE)


def floor_pct(fraction: Decimal) -> Decimal:
    """Fraction -> whole percent, rounded *down* so caps are never exceeded."""
    return (fraction * HUNDRED).to_integral_value(rounding=ROUND_DOWN)


def surplus_discount_band(cover_days: Decimal, target_cover_days: int) -> Decimal:
    """Desired discount percent from how far cover exceeds the target."""
    if target_cover_days <= 0:
        return ZERO
    ratio = cover_days / Decimal(target_cover_days)
    if ratio >= Decimal("4"):
        return Decimal("30")
    if ratio >= Decimal("2.5"):
        return Decimal("20")
    if ratio >= Decimal("1.5"):
        return Decimal("10")
    return ZERO


@dataclass(frozen=True)
class DiscountDecision:
    pct: Decimal | None  # None => no workable discount
    below_margin_floor: bool
    margin_after: Decimal | None
    max_pct_at_floor: Decimal
    breakeven_pct: Decimal


def decide_discount(
    desired_pct: Decimal,
    price: Decimal,
    cost: Decimal,
    margin_floor: Decimal,
    allow_below_floor: bool = False,
) -> DiscountDecision:
    """Cap the desired discount at the margin floor (or at break-even for urgent clearance)."""
    at_floor = floor_pct(max_discount_fraction(price, cost, margin_floor))
    breakeven = floor_pct(max_discount_fraction(price, cost, ZERO))
    cap = breakeven if allow_below_floor else at_floor
    pct = min(D(desired_pct), cap)
    if pct < MIN_DISCOUNT_PCT:
        return DiscountDecision(None, False, None, at_floor, breakeven)
    discounted = D(price) * (ONE - pct / HUNDRED)
    margin_after = margin_pct(discounted, cost)
    return DiscountDecision(pct, pct > at_floor, margin_after, at_floor, breakeven)


# ---------------------------------------------------------------- scoring ---


def confidence_level(eligible_days: int, net_units: Decimal, conflicting: bool = False) -> str:
    """high | medium | low based on history length, volume, and conflicting signals."""
    if conflicting or eligible_days < MIN_HISTORY_DAYS or D(net_units) < 3:
        return "low"
    if eligible_days >= 28 and D(net_units) >= 20:
        return "high"
    return "medium"


def sum_decimal(values: Sequence[Decimal]) -> Decimal:
    total = ZERO
    for v in values:
        total += v
    return total
