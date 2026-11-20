"""The assistants (docs/07): a Supervisor routes each question to a typed tool; the tool reads the
rpt_* views (or a service API) for THIS business only; the answer is a template filled from the
tool's result, so every number spoken is a number that was looked up (grounded by construction).

    guard -> supervisor (regex intents, 0 LLM calls) -> tool -> templated answer + cards/links
                     \\-> no intent matched and ANTHROPIC_API_KEY set -> LLM picks a tool (still templated)

Audiences: ``shopper`` (search, availability, policies, own orders) and ``owner`` (stock, sales,
orders, purchasing, GST, insights). Owner tools each need a permission from the signed context.
Read-only: nothing here changes data.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from blissbiz_common.context import InternalContext
from sqlalchemy import text
from sqlalchemy.orm import Session

from agent_service import insights
from agent_service import metrics as m
from agent_service.facts import SALE_STATUSES, load_facts
from agent_service.text import (
    SIZE_ALIASES,
    SIZES,
    STOP,
    inr,
    period,
    price_range,
    product_term_matches,
    qty,
    stem,
    words,
)

STATUSES = ", ".join(f"'{s}'" for s in SALE_STATUSES)
WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Reply:
    answer: str
    tool: str
    cards: list = field(default_factory=list)
    links: list = field(default_factory=list)
    ui: dict | None = None
    data: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)


@dataclass
class Env:
    db: Session
    ctx: InternalContext
    today: date
    policies: Callable[[], dict]  # store policies, fetched only when needed

    @property
    def b(self) -> str:
        return self.ctx.business_id


def _opts(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value) or {}
    except ValueError:
        return {}


def _opt(options: dict, *keys) -> str | None:
    for k in keys:
        if options.get(k) is not None:
            return str(options[k]).strip().lower()
    return None


# ---- understanding a product question -------------------------------------------------------------
@dataclass
class Want:
    words: set
    color: str | None
    size: str | None
    low: Decimal | None
    high: Decimal | None

    def describe(self) -> str:
        parts = []
        if self.color:
            parts.append(self.color)
        if self.words:
            parts.append(" ".join(sorted(self.words)))
        text_ = " ".join(parts) or "products"
        if self.size:
            text_ += f" in size {self.size.upper()}"
        if self.low is not None and self.high is not None:
            text_ += f" between {inr(self.low)} and {inr(self.high)}"
        elif self.high is not None:
            text_ += f" under {inr(self.high)}"
        elif self.low is not None:
            text_ += f" above {inr(self.low)}"
        return text_


def understand(question: str, rows: list[dict]) -> Want:
    colors = {_opt(r["options"], "color", "colour") for r in rows} - {None}
    sizes = {_opt(r["options"], "size") for r in rows} - {None}
    q = re.sub(r"\b(?:for|on)\s+(?:a\s+)?date(?:\s+night)?\b", " ", question.lower())
    low, high = price_range(q)
    q_wo_price = re.sub(r"(?:rs\.?|₹|inr)?\s*[\d,]+", " ", q)
    tokens = WORD.findall(q_wo_price)
    # A shopper's "blue" also matches catalog labels such as "Floral Blue".
    full_colors = [c for c in colors if re.search(r"\b" + re.escape(c) + r"\b", q)]
    color_words = {word for c in colors for word in WORD.findall(c)}
    color = max(full_colors, key=len) if full_colors else next((t for t in tokens if t in color_words), None)
    size = None
    m_size = re.search(r"\bsize\s+([a-z0-9]+)\b", q)
    if m_size:
        size = SIZE_ALIASES.get(m_size.group(1), m_size.group(1))
    else:
        size = next(
            (
                SIZE_ALIASES.get(t, t)
                for t in tokens
                if t in SIZES and SIZE_ALIASES.get(t, t) in sizes and t not in ("s",)
            ),
            None,
        )
    rest = {
        stem(t)
        for t in tokens
        if t not in STOP
        and t not in WORD.findall(color or "")
        and SIZE_ALIASES.get(t, t) != size
        and not t.isdigit()
        and len(t) > 1
    }
    return Want(rest, color, size, low, high)


def matches(row: dict, want: Want) -> bool:
    if want.color and not set(WORD.findall(want.color)) <= set(
        WORD.findall(_opt(row["options"], "color", "colour") or "")
    ):
        return False
    if want.size and _opt(row["options"], "size") != want.size:
        return False
    price = m.D(row["price"])
    if want.low is not None and price < want.low:
        return False
    if want.high is not None and price > want.high:
        return False
    if want.words:
        haystack = words(row["product_name"], row.get("brand"), row.get("category"), *row["options"].values())
        if not all(product_term_matches(term, haystack) for term in want.words):
            return False
    return True


def group_products(rows: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for r in rows:
        p = out.setdefault(
            r["product_id"],
            {
                "product_id": r["product_id"],
                "name": r["product_name"],
                "slug": r.get("product_slug"),
                "prices": [],
                "available": m.ZERO,
                "variants": 0,
            },
        )
        p["prices"].append(m.D(r["price"]))
        p["available"] += max(m.D(r.get("available", r.get("available_qty"))), m.ZERO)
        p["variants"] += 1
    return list(out.values())


# ---- shopper tools ----------------------------------------------------------------------------------
def catalog_rows(env: Env) -> list[dict]:
    rows = (
        env.db.execute(
            text(
                "SELECT product_id, variant_id, product_name, product_slug, category, brand, sku, options, price, mrp, "
                "stock_unit, available_qty FROM rpt_catalog_public WHERE business_id = :b"
            ),
            {"b": env.b},
        )
        .mappings()
        .all()
    )
    return [{**r, "options": _opts(r["options"])} for r in rows]


def search_products(env: Env, question: str) -> Reply:
    rows = catalog_rows(env)
    want = understand(question, rows)
    hits = [r for r in rows if matches(r, want)]
    products = sorted(group_products(hits), key=lambda p: (p["available"] <= 0, min(p["prices"]), p["name"]))
    in_stock = [p for p in products if p["available"] > 0]
    cards = [
        {
            "type": "product",
            "product_id": p["product_id"],
            "name": p["name"],
            "price": str(min(p["prices"])),
            "in_stock": p["available"] > 0,
            "url": f"/p/{p['slug']}",
        }
        for p in products[:6]
    ]
    query = {
        k: v
        for k, v in {
            "q": " ".join(sorted(want.words)),
            "color": want.color,
            "size": want.size,
            "min_price": want.low,
            "max_price": want.high,
        }.items()
        if v
    }
    link = "/?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "/"
    if not products:
        return Reply(
            f"I couldn't find {want.describe()}. Try a different colour, size or a higher price limit.",
            "search_products",
            links=[{"label": "Browse the shop", "url": "/"}],
        )
    names = ", ".join(f"{p['name']} ({inr(min(p['prices']))})" for p in (in_stock or products)[:3])
    if not in_stock:
        answer = f"I found {len(products)} matching {want.describe()}, but all are out of stock right now: {names}."
    else:
        more = f" and {len(in_stock) - 3} more" if len(in_stock) > 3 else ""
        answer = f"I found {len(in_stock)} in-stock {want.describe()}: {names}{more}."
    return Reply(
        answer,
        "search_products",
        cards,
        [{"label": "See all results", "url": link}],
        data={"products": len(products), "in_stock": len(in_stock)},
    )


def product_options(env: Env, question: str) -> Reply:
    """Return catalog-backed size and colour choices for one or more matching products."""
    rows = catalog_rows(env)
    want = understand(question, rows)
    hits = [row for row in rows if matches(row, want)]
    if not hits:
        return Reply(
            f"I couldn't find {want.describe()} in the catalog.",
            "product_options",
            links=[{"label": "Browse the shop", "url": "/"}],
        )

    products: dict[str, dict] = {}
    for row in hits:
        product = products.setdefault(
            row["product_id"],
            {
                "product_id": row["product_id"],
                "name": row["product_name"],
                "slug": row.get("product_slug"),
                "sizes": set(),
                "colours": set(),
                "available": m.ZERO,
                "prices": [],
            },
        )
        available = max(m.D(row.get("available_qty", row.get("available"))), m.ZERO)
        product["available"] += available
        product["prices"].append(m.D(row["price"]))
        if available > 0:
            size = _opt(row["options"], "size")
            colour = _opt(row["options"], "color", "colour")
            if size:
                product["sizes"].add(size.upper())
            if colour:
                product["colours"].add(colour.title())

    size_order = {
        size.upper(): index for index, size in enumerate(("xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl", "free"))
    }
    details = []
    cards = []
    grounded = []
    for product in sorted(products.values(), key=lambda item: item["name"])[:6]:
        sizes = sorted(product["sizes"], key=lambda value: (size_order.get(value, 99), value))
        colours = sorted(product["colours"])
        if product["available"] <= 0:
            description = f"{product['name']} is currently out of stock"
        else:
            options = []
            if sizes:
                options.append("sizes " + ", ".join(sizes))
            if colours:
                options.append("colours " + ", ".join(colours))
            description = f"{product['name']} is available" + (" in " + "; ".join(options) if options else "")
        details.append(description)
        grounded.append(
            {
                "name": product["name"],
                "sizes": sizes,
                "colours": colours,
                "in_stock": product["available"] > 0,
            }
        )
        cards.append(
            {
                "type": "product",
                "product_id": product["product_id"],
                "name": product["name"],
                "price": str(min(product["prices"])),
                "in_stock": product["available"] > 0,
                "url": f"/p/{product['slug']}",
            }
        )
    return Reply(
        ". ".join(details) + ".",
        "product_options",
        cards,
        data={"products": grounded},
    )


POLICY_TOPICS = (
    ("return", r"return|exchange|send (it )?back"),
    ("refund", r"refund|money back"),
    ("shipping", r"ship|deliver|delivery|courier|how long"),
    ("cancel", r"cancel"),
    ("contact", r"contact|phone|call|email|support|help ?line"),
    ("hours", r"hours|open|timing|close"),
    ("payment", r"pay|cod|cash on delivery|upi|card"),
)


def store_policy(env: Env, question: str) -> Reply:
    p = env.policies()
    q = question.lower()
    topic = next((t for t, rx in POLICY_TOPICS if re.search(rx, q)), "shipping")
    if topic in ("return", "refund"):
        days = p.get("return_window_days")
        answer = (
            f"Returns are accepted within {days} days of delivery." if days else "This shop does not accept returns."
        )
        if days and p.get("return_conditions"):
            answer += f" {p['return_conditions']}"
        if days and p.get("refund_method"):
            answer += f" Refunds go to: {p['refund_method']}."
    elif topic == "cancel":
        hours = p.get("cancel_window_hours")
        answer = (
            f"You can cancel within {hours} hours of ordering, as long as it has not been packed."
            if hours
            else "You can cancel until the order is packed."
        )
    elif topic == "contact":
        bits = [x for x in (p.get("support_phone"), p.get("support_email")) if x]
        answer = (
            f"You can reach the shop at {' or '.join(bits)}." if bits else "The shop has not listed contact details."
        )
    elif topic == "hours":
        hours = p.get("support_hours") or {}
        today = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][env.today.weekday()]
        slot = hours.get(today)
        answer = (
            f"Today the shop's support is open {slot[0]} to {slot[1]}."
            if slot
            else "Support is closed today."
            if hours
            else "The shop has not listed opening hours."
        )
    elif topic == "payment":
        answer = "You pay cash on delivery when your order arrives."
    else:
        lo, hi = p.get("shipping_days_min"), p.get("shipping_days_max")
        answer = f"Delivery takes {lo} to {hi} days." if lo is not None and hi is not None else "We ship across India."
        if p.get("free_shipping_over") is not None:
            answer += (
                f" Orders of {inr(p['free_shipping_over'])} or more ship free; otherwise "
                f"{inr(p.get('flat_shipping_fee') or 0)} shipping applies."
            )
    return Reply(
        answer,
        "store_policy",
        links=[{"label": "All policies", "url": "/policies"}],
        data={"topic": topic},
    )


def my_orders(env: Env, question: str) -> Reply:
    if env.ctx.actor_type != "CUSTOMER" or not env.ctx.actor_id:
        return Reply(
            "Please sign in to see your orders.",
            "my_orders",
            links=[{"label": "Sign in", "url": "/login"}],
        )
    rows = env.db.execute(
        text(
            "SELECT order_id, order_number, MAX(placed_at), MAX(order_status), SUM(line_total) FROM rpt_order_lines "
            "WHERE business_id = :b AND customer_id = :c GROUP BY order_id, order_number ORDER BY MAX(placed_at) DESC"
        ),
        {"b": env.b, "c": env.ctx.actor_id},
    ).all()[:5]
    if not rows:
        return Reply("You have no orders with this shop yet.", "my_orders")
    latest = rows[0]
    answer = f"Your latest order {latest[1]} is {str(latest[3]).lower().replace('_', ' ')}."
    if len(rows) > 1:
        answer += f" You have {len(rows)} recent orders."
    cards = [
        {
            "type": "order",
            "order_id": r[0],
            "number": r[1],
            "status": r[3],
            "url": f"/orders/{r[0]}",
        }
        for r in rows
    ]
    return Reply(answer, "my_orders", cards, [{"label": "My orders", "url": "/orders"}])


# ---- owner tools ----------------------------------------------------------------------------------
def inventory_rows(env: Env) -> list[dict]:
    rows = (
        env.db.execute(
            text(
                "SELECT variant_id, product_id, product_name, product_status, is_active, sku, options, category, "
                "reorder_threshold, price, cost_price, on_hand, reserved, available, stock_value "
                "FROM rpt_inventory_current WHERE business_id = :b"
            ),
            {"b": env.b},
        )
        .mappings()
        .all()
    )
    return [{**r, "options": _opts(r["options"])} for r in rows]


def stock_level(env: Env, question: str) -> Reply:
    rows = [r for r in inventory_rows(env) if r["product_status"] != "ARCHIVED"]
    want = understand(question, rows)
    want = Want(want.words, want.color, want.size, None, None)
    hits = [r for r in rows if matches(r, want)]
    if not hits:
        return Reply(f"I couldn't find {want.describe()} in your catalog.", "stock_level")
    available = sum((m.D(r["available"]) for r in hits), m.ZERO)
    on_hand = sum((m.D(r["on_hand"]) for r in hits), m.ZERO)
    names = sorted({r["product_name"] for r in hits})
    hits.sort(key=lambda r: (r["product_name"], r["sku"]))
    detail = ", ".join(f"{r['sku']} {qty(r['available'])}" for r in hits[:6])
    answer = (
        f"You have {qty(available)} available ({qty(on_hand)} on hand) of {want.describe()} across "
        f"{len(hits)} variant{'s' if len(hits) != 1 else ''}"
        f"{' of ' + names[0] if len(names) == 1 else f' in {len(names)} products'}: {detail}"
        f"{' …' if len(hits) > 6 else ''}."
    )
    cards = [
        {
            "type": "stock",
            "sku": r["sku"],
            "name": r["product_name"],
            "available": qty(r["available"]),
            "on_hand": qty(r["on_hand"]),
        }
        for r in hits[:10]
    ]
    return Reply(
        answer,
        "stock_level",
        cards,
        [{"label": "Inventory", "url": "inventory?q=" + " ".join(sorted(want.words))}],
        data={"available": str(available), "variants": len(hits)},
    )


def low_stock(env: Env, question: str) -> Reply:
    out_only = bool(re.search(r"out of stock|sold out|zero stock|none left", question, re.I))
    rows = env.db.execute(
        text(
            "SELECT product_name, sku, available, reorder_threshold FROM rpt_low_stock WHERE business_id = :b "
            + ("AND available <= 0 " if out_only else "")
            + "ORDER BY available, product_name"
        ),
        {"b": env.b},
    ).all()
    label = "out of stock" if out_only else "low or out of stock"
    if not rows:
        return Reply(f"Nothing is {label} right now.", "low_stock")
    listed = ", ".join(f"{r[0]} {r[1]} ({qty(r[2])} left)" for r in rows[:5])
    answer = f"{len(rows)} item{'s are' if len(rows) != 1 else ' is'} {label}: {listed}{' …' if len(rows) > 5 else ''}."
    cards = [
        {
            "type": "stock",
            "name": r[0],
            "sku": r[1],
            "available": qty(r[2]),
            "reorder_at": qty(r[3]),
        }
        for r in rows[:10]
    ]
    return Reply(
        answer,
        "low_stock",
        cards,
        [{"label": "See low stock", "url": "inventory?low=1"}],
        data={"count": len(rows)},
    )


def sales_summary(env: Env, question: str) -> Reply:
    per = period(question, env.today, "last 7 days")
    row = env.db.execute(
        text(
            "SELECT COALESCE(SUM(revenue),0), COALESCE(SUM(orders),0), COALESCE(SUM(units),0) FROM rpt_sales_daily "
            "WHERE business_id = :b AND sales_date BETWEEN :s AND :e"
        ),
        {"b": env.b, "s": per.start, "e": per.end},
    ).one()
    revenue, orders, units = m.D(row[0]), int(row[1]), m.D(row[2])
    if not orders:
        return Reply(f"No sales {per.label}.", "sales_summary", data={"orders": 0})
    aov = m.q2(revenue / orders)
    answer = (
        f"Sales {per.label}: {inr(revenue)} from {orders} order{'s' if orders != 1 else ''} "
        f"({qty(units)} units), an average of {inr(aov)} per order."
    )
    return Reply(
        answer,
        "sales_summary",
        links=[{"label": "Open dashboard", "url": "insights"}],
        data={"revenue": str(revenue), "orders": orders, "period": per.label},
    )


def top_products(env: Env, question: str) -> Reply:
    per = period(question, env.today, "last 30 days")
    rows = env.db.execute(
        text(
            f"SELECT product_name, SUM(quantity), SUM(line_total) FROM rpt_order_lines WHERE business_id = :b "
            f"AND placed_date BETWEEN :s AND :e AND order_status IN ({STATUSES}) GROUP BY product_name "
            f"ORDER BY 2 DESC, 3 DESC LIMIT 5"
        ),
        {"b": env.b, "s": per.start, "e": per.end},
    ).all()
    if not rows:
        return Reply(f"No sales {per.label}, so there are no best sellers yet.", "top_products")
    listed = "; ".join(f"{n}: {qty(u)} sold ({inr(r)})" for n, u, r in rows)
    cards = [{"type": "product_stat", "name": n, "units": qty(u), "revenue": str(m.q2(m.D(r)))} for n, u, r in rows]
    return Reply(
        f"Your best sellers {per.label}: {listed}.",
        "top_products",
        cards,
        data={"period": per.label},
    )


def orders_summary(env: Env, question: str) -> Reply:
    has_period = bool(re.search(r"today|yesterday|week|month|year|days", question, re.I))
    per = period(question, env.today, "last 30 days")
    sql = "SELECT order_status, COUNT(DISTINCT order_id) FROM rpt_order_lines WHERE business_id = :b "
    params = {"b": env.b}
    if has_period:
        sql += "AND placed_date BETWEEN :s AND :e "
        params |= {"s": per.start, "e": per.end}
    counts = dict(env.db.execute(text(sql + "GROUP BY order_status"), params).all())
    to_pack, to_ship = int(counts.get("CONFIRMED", 0)), int(counts.get("PACKED", 0))
    if re.search(r"pending|to (pack|ship|send|dispatch)|waiting|open|unfulfilled", question, re.I) or not has_period:
        answer = (
            f"{to_pack} order{'s' if to_pack != 1 else ''} to pack and {to_ship} packed, waiting to ship"
            f"{' ' + per.label if has_period else ''}. {int(counts.get('SHIPPED', 0))} are on the way."
        )
        return Reply(
            answer,
            "orders_summary",
            links=[{"label": "Orders to pack", "url": "orders?status=CONFIRMED"}],
            data={"to_pack": to_pack, "to_ship": to_ship},
        )
    total = sum(int(v) for v in counts.values())
    parts = ", ".join(f"{int(v)} {k.lower().replace('_', ' ')}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    return Reply(
        f"{total} order{'s' if total != 1 else ''} {per.label}: {parts}." if total else f"No orders {per.label}.",
        "orders_summary",
        data={"total": total},
    )


def signups(env: Env, question: str) -> Reply:
    per = period(question, env.today, "last 7 days")
    n = int(
        env.db.execute(
            text(
                "SELECT COALESCE(SUM(signups),0) FROM rpt_customer_signups WHERE business_id = :b "
                "AND signup_date BETWEEN :s AND :e"
            ),
            {"b": env.b, "s": per.start, "e": per.end},
        ).scalar()
    )
    return Reply(
        f"{n} customer{'s' if n != 1 else ''} signed up {per.label}.",
        "signups",
        data={"signups": n},
    )


def inventory_value(env: Env, question: str) -> Reply:
    row = env.db.execute(
        text(
            "SELECT COALESCE(SUM(stock_value),0), COALESCE(SUM(on_hand),0), COUNT(*) FROM "
            "rpt_inventory_current WHERE business_id = :b AND on_hand > 0"
        ),
        {"b": env.b},
    ).one()
    return Reply(
        f"Your stock is worth {inr(m.q2(m.D(row[0])))} at cost: {qty(row[1])} units across {row[2]} variants.",
        "inventory_value",
        data={"value": str(m.q2(m.D(row[0])))},
    )


def overdue_purchases(env: Env, question: str) -> Reply:
    rows = env.db.execute(
        text(
            "SELECT number, supplier, expected_date, total FROM rpt_purchasing WHERE business_id = :b "
            "AND is_overdue = 1 ORDER BY expected_date"
        ),
        {"b": env.b},
    ).all()
    if not rows:
        return Reply("No purchase orders are late.", "overdue_purchases")
    listed = ", ".join(f"{n} from {s} (due {str(d)[:10]})" for n, s, d, _ in rows[:5])
    return Reply(
        f"{len(rows)} purchase order{'s are' if len(rows) != 1 else ' is'} late: {listed}.",
        "overdue_purchases",
        links=[{"label": "Purchase orders", "url": "purchase-orders"}],
        data={"count": len(rows)},
    )


def gst_collected(env: Env, question: str) -> Reply:
    per = period(question, env.today, "last 30 days")
    row = env.db.execute(
        text(
            "SELECT COUNT(*), COALESCE(SUM(cgst_total),0), COALESCE(SUM(sgst_total),0), COALESCE(SUM(igst_total),0), "
            "COALESCE(SUM(grand_total),0) FROM rpt_invoices WHERE business_id = :b AND status = 'ISSUED' "
            "AND issued_at >= :s AND issued_at < :e"
        ),
        {"b": env.b, "s": per.start, "e": per.end + timedelta(days=1)},
    ).one()
    n, cg, sg, ig, total = int(row[0]), m.D(row[1]), m.D(row[2]), m.D(row[3]), m.D(row[4])
    gst = cg + sg + ig
    return Reply(
        f"{n} invoice{'s' if n != 1 else ''} {per.label} worth {inr(total)} carried {inr(gst)} GST "
        f"(CGST {inr(cg)}, SGST {inr(sg)}, IGST {inr(ig)}).",
        "gst_collected",
        links=[{"label": "Invoices", "url": "invoices"}],
        data={"gst": str(gst)},
    )


# ---- insights tools -----------------------------------------------------------------------------
REC_KIND = {"reorder": "REORDER", "surplus": "DISCOUNT", "dead": "DEAD_STOCK"}


def recommendations(env: Env, question: str, kind: str | None = None) -> Reply:
    rec_type = REC_KIND.get(kind or "")
    items = insights.recommendations(env.db, env.b, env.today, "OPEN", rec_type)
    label = {
        "REORDER": "to reorder",
        "DISCOUNT": "with surplus stock",
        "DEAD_STOCK": "that are not selling",
    }.get(rec_type, "")
    if not items:
        return Reply(
            f"Nothing {label or 'needs attention'} right now.".replace(
                "Nothing to reorder", "Nothing needs reordering"
            ),
            "recommendations",
            ui={"navigate": "insights"},
        )
    top = items[:3]
    listed = " ".join(f"{i + 1}. {r['title']}: {r['action']}" for i, r in enumerate(top))
    answer = f"{len(items)} item{'s' if len(items) != 1 else ''} {label}. {listed}".strip()
    cards = [
        {
            "type": "recommendation",
            "id": r["id"],
            "title": r["title"],
            "severity": r["severity"],
            "action": r["action"],
        }
        for r in items[:6]
    ]
    return Reply(
        answer,
        "recommendations",
        cards,
        [{"label": "Open recommendations", "url": "insights"}],
        data={
            "counts": insights.counts_by_type(items),
            "recommendations": sorted(items, key=lambda r: -len(words(question) & words(r["title"])))[:8],
            "total": len(items),
            "shown": min(8, len(items)),
        },
    )


def increase_sales(env: Env, question: str) -> Reply:
    items = insights.recommendations(env.db, env.b, env.today, "OPEN")
    reorder = [r for r in items if r["type"] == "REORDER"][:2]
    clear = [r for r in items if r["type"] in ("DISCOUNT", "DEAD_STOCK")][:2]
    best = top_products(env, "last 30 days")
    steps = []
    if reorder:
        steps.append("Restock what sells: " + "; ".join(r["action"] for r in reorder))
    if clear:
        steps.append("Turn idle stock into cash: " + "; ".join(f"{r['title']}: {r['action']}" for r in clear))
    if best.cards:
        steps.append(f"Feature your best seller {best.cards[0]['name']} on the home page")
    if not steps:
        steps.append("Add more products and photos; there is not enough sales history yet for specific advice")
    answer = "Here is a plan: " + " ".join(f"{i + 1}. {s}." for i, s in enumerate(steps))
    return Reply(answer, "increase_sales", links=[{"label": "Open recommendations", "url": "insights"}])


def simulate(env: Env, question: str) -> Reply:
    pct_m = re.search(r"(\d{1,2}(?:\.\d+)?)\s*(?:%|percent)", question, re.I)
    if not pct_m:
        return Reply(
            "Tell me the discount, for example: what if I give 20% off the linen shirt?",
            "simulate_discount",
        )
    rows = [r for r in inventory_rows(env) if r["product_status"] == "PUBLISHED" and r["is_active"]]
    want = understand(re.sub(r"\d+(\.\d+)?\s*(%|percent)", " ", question), rows)
    want = Want(
        want.words - {"what", "if", "give", "off", "discount", "sale", "offer"},
        want.color,
        want.size,
        None,
        None,
    )
    hits = [r for r in rows if matches(r, want)] if want.words or want.color else []
    by_product: dict[str, list] = {}
    for r in hits:
        by_product.setdefault(r["product_id"], []).append(r)
    if not by_product:
        return Reply(
            "Which product? For example: what if I give 20% off the linen shirt?",
            "simulate_discount",
        )
    best = max(by_product.values(), key=lambda rs: len(rs))
    result = insights.simulate_discount(best, Decimal(pct_m.group(1)))
    return Reply(result["answer"], "simulate_discount", data=result)


def inventory_analysis(env: Env, question: str) -> Reply:
    """Bounded, permission-checked evidence for cross-cutting inventory questions."""
    for permission in ("inventory.read", "reports.read", "purchasing.read", "insights.read"):
        env.ctx.require_permission(permission)
    facts = load_facts(env.db, env.b, env.today)
    recs = insights.recommendations(env.db, env.b, env.today, "OPEN")
    query_words = words(question)
    candidates = sorted(
        facts,
        key=lambda f: (
            -len(query_words & words(f.product.name, f.product.sku, f.product.category)),
            f.cover_days if f.cover_days is not None else Decimal("Infinity"),
            f.product.sku,
        ),
    )
    categories = {}
    for f in facts:
        category = categories.setdefault(
            f.product.category or "Uncategorised", {"variants": 0, "available": m.ZERO, "stock_value": m.ZERO}
        )
        category["variants"] += 1
        category["available"] += f.sellable
        category["stock_value"] += f.on_hand * f.product.unit_cost
    rows = [
        {
            "product": f.product.name,
            "sku": f.product.sku,
            "category": f.product.category,
            "on_hand": f.on_hand,
            "reserved": f.reserved,
            "available": f.sellable,
            "incoming": f.incoming,
            "incoming_delayed": f.incoming_delayed,
            "sold_28d": f.net_units,
            "prior_sold_28d": f.prior_net_units,
            "daily_velocity": f.velocity,
            "days_of_cover": f.cover_days,
            "lead_time_days": f.product.lead_time_days,
            "unit_cost": f.product.unit_cost,
            "unit_price": f.product.unit_price,
        }
        for f in candidates[:12]
    ]
    data = {
        "as_of": env.today,
        "definitions": {
            "days_of_cover": "Current available stock divided by daily velocity; incoming purchases are excluded.",
            "incoming": "Outstanding purchases that are not overdue; not yet available to sell.",
            "incoming_delayed": "Overdue purchases, reported separately; not available to sell.",
            "reorder_quantities": "Use only the supplied rule-calculated suggested_qty values.",
        },
        "sales_window_days": 28,
        "total_variants": len(facts),
        "shown_variants": len(rows),
        "selection": "Product/category matches first, then shortest stock cover; sample is not the full catalog.",
        "categories": categories,
        "variants": rows,
        "recommendations": sorted(recs, key=lambda r: -len(query_words & words(r["title"])))[:4],
        "total_recommendations": len(recs),
        "recommendations_shown": min(4, len(recs)),
    }
    return Reply(
        f"Analysed {len(facts)} variants. Showing {len(rows)} prioritised variant records "
        f"and {min(4, len(recs))} recommendations.",
        "inventory_analysis",
        links=[{"label": "View inventory", "url": "inventory"}],
        data=data,
    )


def dashboard(env: Env, question: str) -> Reply:
    d = insights.dashboard(env.db, env.b, env.today)
    k = d["kpis"]
    change = k["revenue"]["change_pct"]
    trend = f" ({'+' if change >= 0 else ''}{change}% vs the 30 days before)" if change is not None else ""
    return Reply(
        f"Last 30 days: {inr(k['revenue']['value'])} revenue{trend} from {k['orders']['value']} orders. "
        f"Your dashboard is on Overview.",
        "open_dashboard",
        data=d,
    )


# ---- routing --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    name: str
    run: Callable
    permission: str | None
    description: str


def _document_reply(env, question, kind):
    from agent_service.documents import reply

    return reply(env, question, kind)


OWNER_TOOLS = {
    t.name: t
    for t in (
        Tool(
            "invoice_export",
            lambda env, q: _document_reply(env, q, "invoice_export"),
            "invoices.read",
            "Collect/export existing issued sales invoices and credit notes as"
            " Excel and printable ZIP. Does not issue invoices.",
        ),
        Tool(
            "registration_pack",
            lambda env, q: _document_reply(env, q, "registration_pack"),
            "business.read",
            "Prepare GST/shop registration documents: draft profile and "
            "conditional evidence checklist workbook; identify missing proofs."
            " No filing or proof verification.",
        ),
        Tool(
            "inventory_export",
            lambda env, q: _document_reply(env, q, "inventory_export"),
            "inventory.read",
            "Download a complete current inventory snapshot as an Excel workbook.",
        ),
        Tool(
            "inventory_analysis",
            inventory_analysis,
            "insights.read",
            "Complex inventory analysis: combine stock, reservations, sales velocity, incoming purchases, "
            "supplier lead times, stockout risk, surplus and margin-safe recommendations. Use for comparisons, "
            "why/how questions, prioritisation, budgets or questions involving several inventory measures.",
        ),
        Tool(
            "simulate_discount",
            simulate,
            "insights.read",
            "What a discount of N% on a product would do to price and margin.",
        ),
        Tool(
            "reorder",
            lambda e, q: recommendations(e, q, "reorder"),
            "insights.read",
            "What should be reordered now.",
        ),
        Tool(
            "surplus",
            lambda e, q: recommendations(e, q, "surplus"),
            "insights.read",
            "Overstocked items worth discounting.",
        ),
        Tool(
            "dead_stock",
            lambda e, q: recommendations(e, q, "dead"),
            "insights.read",
            "Items that are not selling.",
        ),
        Tool("increase_sales", increase_sales, "insights.read", "A plan to increase sales."),
        Tool("recommendations", recommendations, "insights.read", "All open recommendations."),
        Tool("open_dashboard", dashboard, "reports.read", "Open the sales dashboard."),
        Tool("low_stock", low_stock, "inventory.read", "Items that are low or out of stock."),
        Tool("inventory_value", inventory_value, "inventory.read", "Total value of stock at cost."),
        Tool("overdue_purchases", overdue_purchases, "purchasing.read", "Late purchase orders."),
        Tool("gst_collected", gst_collected, "invoices.read", "GST on invoices issued in a period."),
        Tool("signups", signups, "reports.read", "How many customers signed up in a period."),
        Tool("top_products", top_products, "reports.read", "Best-selling products in a period."),
        Tool(
            "sales_summary",
            sales_summary,
            "reports.read",
            "Revenue, orders and average order value in a period.",
        ),
        Tool(
            "orders_summary",
            orders_summary,
            "sales.read",
            "Orders by status; orders waiting to be packed or shipped.",
        ),
        Tool(
            "stock_level",
            stock_level,
            "inventory.read",
            "How many units of a product (colour, size) are in stock.",
        ),
        Tool(
            "store_policy",
            store_policy,
            None,
            "The shop's shipping, returns, cancellation and contact policies.",
        ),
    )
}
SHOPPER_TOOLS = {
    t.name: t
    for t in (
        Tool("my_orders", my_orders, None, "The signed-in customer's own orders and their status."),
        Tool(
            "store_policy",
            store_policy,
            None,
            "Shipping, returns, refunds, cancellation, contact and payment.",
        ),
        Tool(
            "search_products",
            search_products,
            None,
            "Find products by words, colour, size and price.",
        ),
        Tool(
            "product_options",
            product_options,
            None,
            "Check the available sizes, colours or variants of a named product or recent product results.",
        ),
    )
}

OWNER_INTENTS = (
    ("simulate_discount", r"what if|\d+\s*(%|percent)\s*(off|discount)|discount of \d+"),
    ("reorder", r"reorder|restock|order more|should i (buy|order)|running out|replenish"),
    ("surplus", r"overstock|surplus|too much stock|excess stock|overstocked"),
    (
        "dead_stock",
        r"dead stock|not selling|slow[- ]moving|no sales|isn'?t selling|aren'?t selling",
    ),
    (
        "increase_sales",
        r"increase (my )?sales|grow (my )?(sales|business)|boost|more sales|improve sales|sell more",
    ),
    ("open_dashboard", r"dashboard|\bkpis?\b|overview|insights"),
    ("recommendations", r"recommend|suggest|advice|what should i do"),
    ("low_stock", r"low stock|running low|low on|out of stock|sold out|need restocking"),
    (
        "inventory_value",
        r"(inventory|stock) (value|worth)|value of (my )?(stock|inventory)|worth of (my )?stock",
    ),
    (
        "overdue_purchases",
        r"overdue|late (deliveries|delivery|purchase)|purchase orders?|supplier.*late",
    ),
    ("gst_collected", r"\bgst\b|tax (collected|paid|on)|how much tax"),
    ("signups", r"sign ?ups?|signed up|new customers|registrations|registered"),
    ("top_products", r"\btop\b|best ?sell|most (sold|popular)|popular"),
    (
        "sales_summary",
        r"\bsales\b|revenue|earn|turnover|income|how much (did|have) (i|we) (make|made|sell|sold)",
    ),
    ("orders_summary", r"\borders?\b"),
    ("store_policy", r"return policy|refund policy|shipping policy|our polic"),
    (
        "stock_level",
        r"how many|in stock|stock of|\bleft\b|do (i|we) have|units of|stock level|quantity",
    ),
)
SHOPPER_INTENTS = (
    ("my_orders", r"my orders?|order status|where is my order|track|my purchase|did my order"),
    (
        "store_policy",
        r"return|refund|exchange|shipping|deliver|delivery|courier|cancel|warranty|hours|open (today|now)|"
        r"contact|phone|email|support|cash on delivery|\bcod\b|payment|pay with|how long",
    ),
)
OWNER_HELP = (
    "I can answer things like: how many black tees do I have, what is low on stock, sales this week, "
    "best sellers this month, orders to ship, what should I reorder, what is not selling, "
    "what if I give 20% off the linen shirt, GST collected this month."
)
SHOPPER_HELP = (
    "Ask me things like: black dresses under 1500, is the linen shirt available in M, what is the return policy."
)


def route(audience: str, question: str) -> str | None:
    intents = OWNER_INTENTS if audience == "owner" else SHOPPER_INTENTS
    q = question.lower()
    for name, rx in intents:
        if re.search(rx, q):
            return name
    return None


def tools_for(audience: str) -> dict[str, Tool]:
    return OWNER_TOOLS if audience == "owner" else SHOPPER_TOOLS


def llm_tools(audience: str, ctx: InternalContext) -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The user's request restated clearly with obvious spelling corrected. Resolve "
                            "pronouns and follow-ups from the recent conversation when possible. Preserve "
                            "product words, colour, size, price and period."
                        ),
                    }
                },
                "required": ["question"],
            },
        }
        for t in tools_for(audience).values()
        if t.permission is None or ctx.has(t.permission)
    ]


def looks_like_product(question: str) -> bool:
    return bool(set(WORD.findall(question.lower())) - STOP)
