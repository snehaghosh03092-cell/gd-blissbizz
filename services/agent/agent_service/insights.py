"""Insights: recommendations (reorder, surplus discount, dead stock, review), the owner dashboard
and the discount what-if. Every number comes from the rpt_* views through deterministic code; the
LLM, when used at all, only picks which of these to run.

Recommendations are refreshed at most once per business per day (``analysis_runs.run_key``),
lazily on first request. An owner's decision sticks: ACCEPTED/DISMISSED items are not reopened for
14 days and POSTPONED items stay hidden until their snooze date.
"""

import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from app.db.models import utc_now
from app.db.models.agent import AnalysisRun, Recommendation, RecommendationEvent
from blissbiz_common.errors import ApiError
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_service import metrics as m
from agent_service.facts import SALE_STATUSES, load_facts
from agent_service.rules import RULESET_VERSION, SEVERITY_ORDER, generate_candidates
from agent_service.text import inr

STICKY_DAYS = 14
STATUSES = ", ".join(f"'{s}'" for s in SALE_STATUSES)


def refresh(db: Session, business_id: str, as_of: date, trigger: str = "lazy") -> AnalysisRun:
    key = f"{business_id}:{as_of.isoformat()}:{RULESET_VERSION}"
    run = db.scalar(select(AnalysisRun).where(AnalysisRun.run_key == key))
    if run is not None:
        return run
    run = AnalysisRun(business_id=business_id, run_key=key, as_of=as_of, trigger=trigger, status="running")
    db.add(run)
    try:
        db.flush()
    except IntegrityError:  # a parallel request is computing the same day
        db.rollback()
        return db.scalar(select(AnalysisRun).where(AnalysisRun.run_key == key))
    facts = load_facts(db, business_id, as_of)
    candidates = generate_candidates(facts)
    existing = {
        r.dedup_key: r for r in db.scalars(select(Recommendation).where(Recommendation.business_id == business_id))
    }
    seen, created, updated = set(), 0, 0
    now = utc_now()
    for c in candidates:
        seen.add(c.dedup_key)
        rec = existing.get(c.dedup_key)
        payload = dict(
            rec_type=c.rec_type,
            severity=c.severity,
            confidence=c.confidence,
            title=c.title[:255],
            summary=c.summary,
            action=c.action,
            suggested_qty=c.suggested_qty,
            suggested_discount_pct=c.suggested_discount_pct,
            value_at_risk=c.value_at_risk,
            evidence=c.evidence,
            variant_id=c.product_id,
            run_id=run.id,
        )
        if rec is None:
            db.add(Recommendation(business_id=business_id, dedup_key=c.dedup_key, status="OPEN", **payload))
            created += 1
            continue
        decided_recently = (
            rec.status in ("ACCEPTED", "DISMISSED")
            and rec.status_changed_at
            and rec.status_changed_at > now - timedelta(days=STICKY_DAYS)
        )
        snoozed = rec.status == "POSTPONED" and rec.snooze_until and rec.snooze_until > as_of
        if decided_recently or snoozed:
            continue
        for k, v in payload.items():
            setattr(rec, k, v)
        if rec.status != "OPEN":
            db.add(
                RecommendationEvent(
                    recommendation_id=rec.id,
                    business_id=business_id,
                    from_status=rec.status,
                    to_status="OPEN",
                    comment="Condition is back",
                )
            )
            rec.status, rec.status_changed_at = "OPEN", now
        updated += 1
    resolved = 0
    for key, rec in existing.items():
        if key not in seen and rec.status == "OPEN":  # the problem went away on its own
            db.add(
                RecommendationEvent(
                    recommendation_id=rec.id,
                    business_id=business_id,
                    from_status="OPEN",
                    to_status="RESOLVED",
                    comment="No longer applies",
                )
            )
            rec.status, rec.status_changed_at = "RESOLVED", now
            resolved += 1
    run.products_analyzed, run.created_count, run.updated_count, run.resolved_count = (
        len(facts),
        created,
        updated,
        resolved,
    )
    run.status, run.finished_at = "done", utc_now()
    db.commit()
    return run


def rec_out(r: Recommendation, variant: dict | None = None) -> dict:
    return {
        "id": r.id,
        "type": r.rec_type,
        "severity": r.severity,
        "confidence": r.confidence,
        "status": r.status,
        "title": r.title,
        "summary": r.summary,
        "action": r.action,
        "variant_id": r.variant_id,
        "variant": variant,
        "suggested_qty": str(r.suggested_qty) if r.suggested_qty is not None else None,
        "suggested_discount_pct": str(r.suggested_discount_pct) if r.suggested_discount_pct is not None else None,
        "value_at_risk": str(r.value_at_risk) if r.value_at_risk is not None else None,
        "evidence": r.evidence,
        "discount_application": (r.evidence or {}).get("discount_application"),
        "snooze_until": r.snooze_until.isoformat() if r.snooze_until else None,
    }


def recommendations(
    db: Session,
    business_id: str,
    as_of: date,
    status: str | None = "OPEN",
    rec_type: str | None = None,
) -> list[dict]:
    refresh(db, business_id, as_of)
    query = select(Recommendation).where(Recommendation.business_id == business_id)
    if status:
        if status == "OPEN":
            query = query.where(
                or_(
                    Recommendation.status == "OPEN",
                    and_(Recommendation.status == "ACCEPTED", Recommendation.rec_type == "DISCOUNT"),
                )
            )
        else:
            query = query.where(Recommendation.status == status)
    if rec_type:
        query = query.where(Recommendation.rec_type == rec_type)
    rows = list(db.scalars(query))
    if status == "OPEN":
        # Earlier releases recorded acceptance without changing prices. Keep those actionable.
        rows = [r for r in rows if r.status == "OPEN" or not (r.evidence or {}).get("discount_application")]
    rows.sort(key=lambda r: (SEVERITY_ORDER.get(r.severity, 9), -(r.value_at_risk or 0), r.title))
    variants = {}
    if rows:
        for item in db.execute(
            text(
                "SELECT variant_id, product_name, product_slug, sku, options "
                "FROM rpt_catalog_public WHERE business_id=:b"
            ),
            {"b": business_id},
        ).mappings():
            variant = dict(item)
            variant["options"] = (
                json.loads(variant["options"]) if isinstance(variant["options"], str) else (variant["options"] or {})
            )
            variants[variant["variant_id"]] = variant
    return [rec_out(r, variants.get(r.variant_id)) for r in rows]


def decide(
    db: Session,
    business_id: str,
    rec_id: str,
    to: str,
    actor_id: str | None,
    comment: str | None,
    snooze_days: int | None,
    today: date,
    *,
    discount_apply=None,
    discount_allowed: bool = False,
) -> dict:
    rec = db.scalar(
        select(Recommendation)
        .where(Recommendation.business_id == business_id, Recommendation.id == rec_id)
        .with_for_update()
    )
    if rec is None:
        raise ApiError(404, "not_found", "Not found.")
    if to == "ACCEPTED" and rec.rec_type == "DISCOUNT":
        if not discount_allowed:
            raise ApiError(403, "forbidden", "Applying a discount requires catalog editing permission.")
        if (rec.evidence or {}).get("discount_application"):
            return rec_out(rec)
        # Previously accepted (decision-only) recommendations can also be applied once.
        if rec.status not in ("OPEN", "POSTPONED", "ACCEPTED"):
            raise ApiError(409, "invalid_transition", "This recommendation is no longer available.")
        if discount_apply is None:
            raise ApiError(503, "service_unavailable", "The discount could not be applied. Try again.")
        application = discount_apply(rec)
        rec.evidence = {**(rec.evidence or {}), "discount_application": application}
        db.add(
            RecommendationEvent(
                recommendation_id=rec.id,
                business_id=business_id,
                user_id=actor_id,
                from_status=rec.status,
                to_status="ACCEPTED",
                comment=f"Applied {application['discount_pct']}% discount: "
                f"{application['original_price']} -> {application['selling_price']}",
            )
        )
        rec.status, rec.status_changed_at = "ACCEPTED", utc_now()
        db.commit()
        return rec_out(rec)
    if rec.status == to:
        return rec_out(rec)
    legacy_discount = (
        rec.status == "ACCEPTED" and rec.rec_type == "DISCOUNT" and not (rec.evidence or {}).get("discount_application")
    )
    if rec.status not in ("OPEN", "POSTPONED") and not legacy_discount:
        raise ApiError(409, "invalid_transition", f"This recommendation is already {rec.status.lower()}.")
    db.add(
        RecommendationEvent(
            recommendation_id=rec.id,
            business_id=business_id,
            user_id=actor_id,
            from_status=rec.status,
            to_status=to,
            comment=comment,
        )
    )
    rec.status, rec.status_changed_at = to, utc_now()
    rec.snooze_until = today + timedelta(days=snooze_days or 7) if to == "POSTPONED" else None
    db.commit()
    return rec_out(rec)


def _sum(db, sql, params) -> Decimal:
    return m.D(db.execute(text(sql), params).scalar() or 0)


def dashboard(db: Session, business_id: str, today: date) -> dict:
    """30-day KPIs against the 30 days before, daily trend, top products, categories, stock health."""
    start, prior = today - timedelta(days=29), today - timedelta(days=59)
    p = {"b": business_id, "s": start, "e": today, "ps": prior, "pe": start - timedelta(days=1)}

    def kpis(s, e):
        row = db.execute(
            text(
                "SELECT COALESCE(SUM(revenue),0), COALESCE(SUM(orders),0), COALESCE(SUM(units),0) "
                "FROM rpt_sales_daily WHERE business_id=:b AND sales_date BETWEEN :s AND :e"
            ),
            {"b": business_id, "s": s, "e": e},
        ).one()
        revenue, orders, units = m.D(row[0]), int(row[1]), m.D(row[2])
        return {
            "revenue": revenue,
            "orders": orders,
            "units": units,
            "aov": m.q2(revenue / orders) if orders else m.ZERO,
        }

    now_k, before_k = kpis(start, today), kpis(prior, start - timedelta(days=1))

    def change(a, b):
        return None if not b else float(round((m.D(a) - m.D(b)) / m.D(b) * 100, 1))

    daily = {
        str(d)[:10]: (m.D(r), int(o))
        for d, r, o in db.execute(
            text(
                "SELECT sales_date, revenue, orders FROM rpt_sales_daily "
                "WHERE business_id=:b AND sales_date BETWEEN :s AND :e"
            ),
            p,
        )
    }
    trend = []
    for n in range(30):
        d = (start + timedelta(days=n)).isoformat()
        rev, orders = daily.get(d, (m.ZERO, 0))
        trend.append({"date": d, "revenue": str(m.q2(rev)), "orders": orders})
    top = db.execute(
        text(
            f"SELECT product_name, SUM(quantity) AS units, SUM(line_total) AS revenue FROM rpt_order_lines "
            f"WHERE business_id=:b AND placed_date BETWEEN :s AND :e AND order_status IN ({STATUSES}) "
            f"GROUP BY product_name ORDER BY units DESC, revenue DESC LIMIT 5"
        ),
        p,
    ).all()
    cats = db.execute(
        text(
            f"SELECT COALESCE(c.category, 'Uncategorised'), SUM(l.line_total) FROM rpt_order_lines l "
            f"LEFT JOIN rpt_inventory_current c ON c.variant_id = l.variant_id AND c.business_id = l.business_id "
            f"WHERE l.business_id=:b AND l.placed_date BETWEEN :s AND :e AND l.order_status IN ({STATUSES}) "
            f"GROUP BY COALESCE(c.category, 'Uncategorised') ORDER BY 2 DESC"
        ),
        p,
    ).all()
    stock = db.execute(
        text(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN available <= 0 THEN 1 ELSE 0 END), 0) FROM rpt_low_stock "
            "WHERE business_id=:b"
        ),
        p,
    ).one()
    value = _sum(db, "SELECT SUM(stock_value) FROM rpt_inventory_current WHERE business_id=:b", p)
    signups = int(
        _sum(
            db,
            "SELECT SUM(signups) FROM rpt_customer_signups WHERE business_id=:b AND signup_date BETWEEN :s AND :e",
            p,
        )
    )
    return {
        "period": {"from": start.isoformat(), "to": today.isoformat()},
        "kpis": {
            k: {
                "value": str(m.q2(v)) if isinstance(v, Decimal) else v,
                "change_pct": change(v, before_k[k]),
            }
            for k, v in now_k.items()
        },
        "trend": trend,
        "top_products": [{"name": n, "units": str(u), "revenue": str(m.q2(m.D(r)))} for n, u, r in top],
        "categories": [{"name": n, "revenue": str(m.q2(m.D(r)))} for n, r in cats],
        "stock": {
            "low_or_out": int(stock[0]),
            "out": int(stock[1]),
            "inventory_value": str(m.q2(value)),
        },
        "signups_30d": signups,
    }


def simulate_discount(rows: list[dict], pct: Decimal) -> dict:
    """What a discount does to price and margin, and how many more units would keep gross profit the
    same. Capped at the margin floor; refused when the discount would sell at a loss."""
    if not rows:
        raise ApiError(404, "not_found", "I couldn't find that product.")
    pct = m.D(pct)
    if pct <= 0 or pct >= 90:
        raise ApiError(422, "validation_error", "Choose a discount between 1 and 89 percent.")
    price = sum((m.D(r["price"]) for r in rows), m.ZERO) / len(rows)
    cost = sum((m.D(r["cost_price"]) for r in rows), m.ZERO) / len(rows)
    new_price = m.q2(price * (1 - pct / 100))
    margin_now, margin_new = m.margin_pct(price, cost), m.margin_pct(new_price, cost)
    floor_pct = m.floor_pct(m.max_discount_fraction(price, cost, Decimal("0.20")))
    unit_profit_now, unit_profit_new = price - cost, new_price - cost
    extra = None
    if unit_profit_new > 0:
        extra = float(round((unit_profit_now / unit_profit_new - 1) * 100, 1))
    name = rows[0]["product_name"]
    if unit_profit_new <= 0:
        verdict = f"At {pct:g}% off, {name} would sell below cost ({inr(new_price)} vs cost {inr(cost)}). Not advised."
    else:
        verdict = (
            f"At {pct:g}% off, {name} goes from {inr(price)} to {inr(new_price)}; margin falls from "
            f"{m.num(margin_now * 100, 1)}% to {m.num(margin_new * 100, 1)}%. You would need about "
            f"{extra}% more units to earn the same gross profit."
        )
        if pct > floor_pct:
            verdict += f" That is deeper than the {floor_pct}% the 20% margin floor allows."
    return {
        "product": name,
        "variants": len(rows),
        "discount_pct": str(pct),
        "price": str(m.q2(price)),
        "new_price": str(new_price),
        "cost": str(m.q2(cost)),
        "margin_pct": m.num((margin_now or 0) * 100, 1),
        "new_margin_pct": m.num((margin_new or 0) * 100, 1),
        "extra_units_needed_pct": extra,
        "max_discount_at_floor_pct": str(floor_pct),
        "answer": verdict,
    }


def counts_by_type(items: list[dict]) -> dict:
    out: dict = defaultdict(int)
    for r in items:
        out[r["type"]] += 1
    return dict(out)
