"""Rule engine: ProductFacts -> explainable recommendation candidates. Deterministic, no LLM.

Ported from the earlier insights agent (test1 candidates.py). Kept: REORDER, DISCOUNT (surplus), DEAD_STOCK, REVIEW.
Dropped: EXPIRY_ACTION (this schema has no lots/expiry dates). Discounts never break the margin floor.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agent_service import metrics as m
from agent_service.facts import ProductFacts

RULESET_VERSION = "1.0"
MIN_EXCESS_VALUE = Decimal("25.00")
FALLING_DEMAND_THRESHOLD = Decimal("-0.4")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Candidate:
    rec_type: str
    severity: str
    confidence: str
    dedup_key: str
    product_id: int
    title: str
    summary: str
    action: str
    suggested_discount_pct: Decimal | None = None
    suggested_qty: Decimal | None = None
    value_at_risk: Decimal | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def _fmt(x: Decimal | None, places: int = 1) -> str:
    if x is None:
        return "n/a"
    text = f"{round(m.D(x), places):.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _money(x: Decimal | None) -> str:
    return f"₹{m.D(x):,.2f}"


def _base_evidence(f: ProductFacts) -> dict[str, Any]:
    p = f.product
    return {
        "sku": p.sku,
        "product": p.name,
        "category": p.category,
        "as_of": f.as_of.isoformat(),
        "window_days": f.window_days,
        "eligible_days": f.eligible_days,
        "units_sold_window": m.num(f.net_units, 0),
        "velocity_per_day": m.num(f.velocity, 3),
        "prior_velocity_per_day": m.num(f.prior_velocity, 3),
        "trend_pct": m.num(f.trend * 100 if f.trend is not None else None, 1),
        "on_hand_qty": m.num(f.on_hand, 0),
        "incoming_qty": m.num(f.incoming, 0),
        "incoming_delayed_qty": m.num(f.incoming_delayed, 0),
        "calculation_version": m.CALCULATION_VERSION,
    }


def _discount_text(d: m.DiscountDecision, price: Decimal, cost: Decimal) -> str:
    if d.pct is None:
        return (
            f"A discount is not viable: price {_money(price)} vs cost {_money(cost)} leaves no room above the "
            f"margin floor (max {_fmt(d.max_pct_at_floor, 0)}%). Consider bundling or cross-promotion instead."
        )
    return f"Offer a {_fmt(d.pct, 0)}% discount (margin after discount {_fmt((d.margin_after or m.ZERO) * 100, 1)}%)."


# ------------------------------------------------------------------ reorder -
def reorder_candidate(f: ProductFacts) -> Candidate | None:
    p = f.product
    if not p.is_active:
        return None
    # if out of stock and nothing sold recently (stock-out hides demand), use the prior-window rate
    rate, used_prior = f.rate, False
    if rate <= m.ZERO and f.sellable <= m.ZERO and f.prior_velocity and f.prior_velocity > m.ZERO:
        rate, used_prior = f.prior_velocity, True
    if rate <= m.ZERO:
        return None
    rop = m.reorder_point(rate, p.lead_time_days, p.safety_stock)
    if f.sellable + f.incoming >= rop:
        return None

    cover = m.days_of_cover(f.sellable, rate)
    out_of_stock = f.sellable <= m.ZERO
    severity = "critical" if (out_of_stock or (cover is not None and cover <= p.lead_time_days)) else "high"
    target = max(m.target_stock(rate, p.target_cover_days, p.safety_stock), rop)
    qty = m.suggested_reorder_qty(target, f.sellable, f.incoming, p.min_order_qty, p.pack_size)
    if qty <= m.ZERO:
        return None
    est_cost = qty * p.unit_cost
    stockout = m.projected_stockout_date(f.as_of, cover)
    conflicting = f.trend is not None and f.trend <= FALLING_DEMAND_THRESHOLD

    warnings: list[str] = []
    if conflicting:
        warnings.append(
            f"Demand fell {_fmt(abs(f.trend) * 100, 0)}% versus the prior period; consider a smaller order."
        )
    if used_prior:
        warnings.append("No recent sales because the product is out of stock; prior-period demand was used.")
    if f.eligible_days < m.MIN_HISTORY_DAYS:
        warnings.append(f"Less than {m.MIN_HISTORY_DAYS} days of history; demand estimate is uncertain.")
    if f.incoming_delayed > m.ZERO:
        warnings.append(
            f"{_fmt(f.incoming_delayed, 0)} units on an overdue purchase order are NOT counted as incoming."
        )
    if p.supplier_id is None:
        warnings.append("No supplier is assigned to this product.")

    ev = _base_evidence(f)
    ev.update(
        {
            "days_of_cover": m.num(cover, 1),
            "lead_time_days": p.lead_time_days,
            "supplier": p.supplier_name,
            "safety_stock": m.num(p.safety_stock, 0),
            "reorder_point": m.num(rop, 1),
            "target_stock": m.num(target, 1),
            "projected_stockout_date": stockout.isoformat() if stockout else None,
            "estimated_cost": m.num(est_cost, 2),
            "warnings": warnings,
        }
    )
    when = (
        "is out of stock"
        if out_of_stock
        else f"has about {_fmt(cover)} days of cover (lead time {p.lead_time_days} days)"
    )
    action = f"Order {_fmt(qty, 0)} units (~{_money(est_cost)}) to reach {p.target_cover_days} days of cover."
    if f.incoming_delayed > m.ZERO:
        action += " Follow up on the overdue purchase order."
    if conflicting:
        action += " Demand is falling, so confirm the quantity before ordering."
    return Candidate(
        "REORDER",
        severity,
        m.confidence_level(f.eligible_days, f.net_units, conflicting),
        f"REORDER:{p.id}",
        p.id,
        f"Reorder {p.name} ({'stock-out' if out_of_stock else 'low stock'})",
        f"{p.name} {when}. On hand {_fmt(f.sellable, 0)} + incoming {_fmt(f.incoming, 0)} is below the reorder "
        f"point of {_fmt(rop, 0)}.",
        action,
        suggested_qty=qty,
        evidence=ev,
    )


# ------------------------------------------------------------------ surplus -
def surplus_candidate(f: ProductFacts) -> Candidate | None:
    p = f.product
    if not p.is_active or f.velocity is None or f.velocity <= m.ZERO or f.cover_days is None:
        return None
    cover, target = f.cover_days, f.target_stock
    excess = m.excess_qty(f.sellable, f.incoming, target)
    excess_value = excess * p.unit_cost
    if excess <= m.ZERO or excess_value < MIN_EXCESS_VALUE:
        return None
    if cover <= Decimal(p.target_cover_days) * m.SURPLUS_COVER_MULTIPLIER:
        return None
    ratio = cover / Decimal(p.target_cover_days)
    severity = "high" if ratio >= 4 else "medium" if ratio >= Decimal("2.5") else "low"
    d = m.decide_discount(
        m.surplus_discount_band(cover, p.target_cover_days),
        p.unit_price,
        p.unit_cost,
        p.margin_floor_pct,
    )
    ev = _base_evidence(f)
    ev.update(
        {
            "days_of_cover": m.num(cover, 1),
            "target_cover_days": p.target_cover_days,
            "target_stock": m.num(target, 1),
            "excess_qty": m.num(excess, 0),
            "excess_value": m.num(excess_value, 2),
            "unit_price": m.num(p.unit_price, 2),
            "unit_cost": m.num(p.unit_cost, 2),
            "margin_pct": m.num((m.margin_pct(p.unit_price, p.unit_cost) or m.ZERO) * 100, 1),
            "margin_floor_pct": m.num(p.margin_floor_pct * 100, 1),
            "max_discount_pct_at_floor": m.num(d.max_pct_at_floor, 0),
            "days_since_last_sale": f.days_since_last_sale,
            "warnings": [],
        }
    )
    if f.eligible_days < m.MIN_HISTORY_DAYS:
        ev["warnings"].append(f"Less than {m.MIN_HISTORY_DAYS} days of history; demand estimate is uncertain.")
    if f.trend is not None and f.trend >= Decimal("0.3"):
        ev["warnings"].append(f"Demand is rising ({_fmt(f.trend * 100, 0)}%); surplus may sell down on its own.")
    return Candidate(
        "DISCOUNT",
        severity,
        m.confidence_level(f.eligible_days, f.net_units),
        f"DISCOUNT:{p.id}",
        p.id,
        f"Surplus: {p.name} ({_fmt(cover, 0)} days of cover)",
        f"{p.name} has {_fmt(cover, 0)} days of cover versus a {p.target_cover_days}-day target; about "
        f"{_fmt(excess, 0)} units ({_money(excess_value)} at cost) are surplus.",
        _discount_text(d, p.unit_price, p.unit_cost) + " Pause reorders until cover drops below target.",
        suggested_discount_pct=d.pct,
        value_at_risk=excess_value,
        evidence=ev,
    )


# --------------------------------------------------------------- dead stock -
def dead_stock_candidate(f: ProductFacts) -> Candidate | None:
    p = f.product
    if f.sellable <= m.ZERO:
        return None
    discontinued = not p.is_active
    idle = f.days_since_last_sale if f.days_since_last_sale is not None else f.days_in_stock
    no_recent_sales = f.velocity is None or f.velocity <= m.ZERO
    if not discontinued and (not no_recent_sales or idle is None or idle < p.dead_stock_days):
        return None
    idle = idle or 0
    severe = idle >= 2 * p.dead_stock_days
    d = m.decide_discount(
        Decimal("40") if (severe or discontinued) else Decimal("30"),
        p.unit_price,
        p.unit_cost,
        p.margin_floor_pct,
    )
    value = f.sellable * p.unit_cost
    ev = _base_evidence(f)
    ev.update(
        {
            "days_since_last_sale": f.days_since_last_sale,
            "days_in_stock": f.days_in_stock,
            "dead_stock_days": p.dead_stock_days,
            "discontinued": discontinued,
            "stock_value": m.num(value, 2),
            "unit_price": m.num(p.unit_price, 2),
            "unit_cost": m.num(p.unit_cost, 2),
            "max_discount_pct_at_floor": m.num(d.max_pct_at_floor, 0),
            "warnings": [],
        }
    )
    if discontinued:
        title = f"Clear discontinued stock: {p.name}"
        summary = f"{p.name} is discontinued but {_fmt(f.sellable, 0)} units ({_money(value)}) remain in stock."
    else:
        title = f"Dead stock: {p.name} (no sales for {idle} days)"
        summary = (
            f"{p.name} has not sold for {idle} days (threshold {p.dead_stock_days}); "
            f"{_fmt(f.sellable, 0)} units ({_money(value)} at cost) are tied up."
        )
    return Candidate(
        "DEAD_STOCK",
        "high" if (severe or (discontinued and value >= 500)) else "medium",
        "high" if idle >= p.dead_stock_days else "medium",
        f"DEAD_STOCK:{p.id}",
        p.id,
        title,
        summary,
        _discount_text(d, p.unit_price, p.unit_cost) + " Do not reorder.",
        suggested_discount_pct=d.pct,
        value_at_risk=value,
        evidence=ev,
    )


# ------------------------------------------------------------------- review -
def review_candidates(f: ProductFacts) -> list[Candidate]:
    p, out = f.product, []
    if p.is_active and f.sellable > m.ZERO and f.net_units <= m.ZERO and f.eligible_days < m.MIN_HISTORY_DAYS:
        ev = _base_evidence(f)
        ev["warnings"] = ["Not enough sales history to recommend reorder or discount actions."]
        out.append(
            Candidate(
                "REVIEW",
                "low",
                "low",
                f"REVIEW:{p.id}:new",
                p.id,
                f"Not enough data: {p.name}",
                f"{p.name} has only {f.eligible_days} day(s) of history and no sales yet.",
                "Monitor sales for two weeks; consider an introductory promotion if it stays flat.",
                evidence=ev,
            )
        )
    if f.incoming_delayed > m.ZERO:
        ev = _base_evidence(f)
        ev["warnings"] = ["Overdue supply is excluded from stock-cover calculations."]
        out.append(
            Candidate(
                "REVIEW",
                "medium",
                "high",
                f"REVIEW:{p.id}:overdue_po",
                p.id,
                f"Overdue purchase order: {p.name}",
                f"{_fmt(f.incoming_delayed, 0)} units of {p.name} were expected but have not arrived.",
                "Contact the supplier for a new delivery date.",
                evidence=ev,
            )
        )
    return out


def generate_candidates(facts: Sequence[ProductFacts]) -> list[Candidate]:
    out: list[Candidate] = []
    for f in facts:
        r = reorder_candidate(f)
        if r:
            out.append(r)
        dead = dead_stock_candidate(f)
        if dead:
            out.append(dead)
        else:  # dead stock advice supersedes a generic surplus discount
            s = surplus_candidate(f)
            if s:
                out.append(s)
        out.extend(review_candidates(f))
    return out
