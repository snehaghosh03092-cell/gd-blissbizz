"""Per-variant facts for the recommendation rules, read ONLY from the rpt_* views.

Demand comes from sold order lines (28-day window vs the 28 days before), stock from current
balances (sellable = on hand - held for orders), stock on its way from open purchase orders.
Shop-wide defaults stand in for settings the schema does not hold yet (lead time when no
supplier is known, cover target, margin floor, dead-stock threshold)."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from agent_service import metrics as m

WINDOW_DAYS = 28
DEFAULT_LEAD_TIME = 7
TARGET_COVER_DAYS = 30
MARGIN_FLOOR = Decimal("0.20")
DEAD_STOCK_DAYS = 60
SALE_STATUSES = ("CONFIRMED", "PACKED", "SHIPPED", "DELIVERED", "RETURN_REQUESTED", "RETURNED")


@dataclass
class ProductPolicy:
    id: str  # variant id
    sku: str
    name: str
    category: str | None
    is_active: bool
    unit_price: Decimal
    unit_cost: Decimal
    supplier_id: str | None
    supplier_name: str | None
    lead_time_days: int
    safety_stock: Decimal
    target_cover_days: int = TARGET_COVER_DAYS
    margin_floor_pct: Decimal = MARGIN_FLOOR
    dead_stock_days: int = DEAD_STOCK_DAYS
    pack_size: int = 1
    min_order_qty: Decimal = m.ONE
    product_id: str | None = None


@dataclass
class ProductFacts:
    product: ProductPolicy
    as_of: date
    window_days: int
    on_hand: Decimal = m.ZERO
    reserved: Decimal = m.ZERO
    incoming: Decimal = m.ZERO
    incoming_delayed: Decimal = m.ZERO
    net_units: Decimal = m.ZERO
    prior_net_units: Decimal = m.ZERO
    eligible_days: int = 0
    velocity: Decimal | None = None
    prior_velocity: Decimal | None = None
    last_sale_date: date | None = None
    first_seen: date | None = None

    @property
    def sellable(self) -> Decimal:
        return m.available_qty(self.on_hand, self.reserved)

    @property
    def rate(self) -> Decimal:
        return self.velocity if self.velocity is not None else m.ZERO

    @property
    def trend(self) -> Decimal | None:
        return m.trend_pct(self.velocity, self.prior_velocity)

    @property
    def cover_days(self) -> Decimal | None:
        return m.days_of_cover(self.sellable, self.velocity)

    @property
    def days_since_last_sale(self) -> int | None:
        return None if self.last_sale_date is None else max((self.as_of - self.last_sale_date).days, 0)

    @property
    def days_in_stock(self) -> int | None:
        return None if self.first_seen is None else max((self.as_of - self.first_seen).days, 0)

    @property
    def target_stock(self) -> Decimal:
        p = self.product
        return m.target_stock(self.rate, p.target_cover_days, p.safety_stock)


def as_date(value) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def load_facts(db: Session, business_id: str, as_of: date, history_start: date | None = None) -> list[ProductFacts]:
    start = as_of - timedelta(days=WINDOW_DAYS)
    prior_start = start - timedelta(days=WINDOW_DAYS)
    statuses = ", ".join(f"'{s}'" for s in SALE_STATUSES)
    inventory = db.execute(
        text(
            "SELECT variant_id, product_id, product_name, sku, category, product_status, is_active, price, "
            "cost_price, reorder_threshold, on_hand, reserved FROM rpt_inventory_current WHERE business_id = :b"
        ),
        {"b": business_id},
    ).all()
    sales = {
        r[0]: r[1:]
        for r in db.execute(
            text(
                f"SELECT variant_id, "
                f"SUM(CASE WHEN placed_date >= :s AND placed_date < :a THEN quantity ELSE 0 END), "
                f"SUM(CASE WHEN placed_date >= :p AND placed_date < :s THEN quantity ELSE 0 END), "
                f"MAX(placed_date), MIN(placed_date) "
                f"FROM rpt_order_lines WHERE business_id = :b AND order_status IN ({statuses}) GROUP BY variant_id"
            ),
            {"b": business_id, "s": start, "a": as_of, "p": prior_start},
        )
    }
    incoming = {
        r[0]: r[1:]
        for r in db.execute(
            text(
                "SELECT variant_id, incoming_qty, delayed_qty, lead_time_days, supplier FROM rpt_incoming_stock "
                "WHERE business_id = :b"
            ),
            {"b": business_id},
        )
    }
    out = []
    for (
        vid,
        pid,
        name,
        sku,
        category,
        status,
        active,
        price,
        cost,
        threshold,
        on_hand,
        reserved,
    ) in inventory:
        if status == "DRAFT":
            continue  # never on sale: nothing to recommend
        cur, prior, last, first = sales.get(vid, (0, 0, None, None))
        inc, delayed, lead, supplier = incoming.get(vid, (0, 0, None, None))
        first_d = as_date(first) or history_start
        eligible = m.eligible_days_in_window(first_d, start, as_of - timedelta(days=1))
        prior_eligible = m.eligible_days_in_window(first_d, prior_start, start - timedelta(days=1))
        policy = ProductPolicy(
            id=vid,
            sku=sku,
            name=f"{name} ({sku})",
            category=category,  # several variants share a name
            is_active=status == "PUBLISHED" and bool(active),
            unit_price=m.D(price),
            unit_cost=m.D(cost),
            supplier_id=supplier,
            supplier_name=supplier,
            lead_time_days=int(lead or DEFAULT_LEAD_TIME),
            safety_stock=m.D(threshold),
            product_id=pid,
        )
        out.append(
            ProductFacts(
                product=policy,
                as_of=as_of,
                window_days=WINDOW_DAYS,
                on_hand=m.D(on_hand),
                reserved=m.D(reserved),
                incoming=m.D(inc) - m.D(delayed),
                incoming_delayed=m.D(delayed),
                net_units=m.D(cur),
                prior_net_units=m.D(prior),
                eligible_days=eligible,
                velocity=m.average_daily_sales(m.D(cur), eligible),
                prior_velocity=m.average_daily_sales(m.D(prior), prior_eligible),
                last_sale_date=as_date(last),
                first_seen=first_d,
            )
        )
    return out
