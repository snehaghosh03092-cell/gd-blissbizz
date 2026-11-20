"""Day-by-day business simulator: produces row dicts for every table of ONE tenant.

All stock changes go through ``_ledger`` so balances equal the ledger by construction; reservations, payments,
fulfilment, returns, refunds, purchasing and invoices follow the lifecycle documented in docs/04 and docs/06.
Times are naive UTC (like the database); demand follows India local time (UTC+5:30).
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import random
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.core.gstin import with_checksum
from app.core.money import ZERO, D, round2, round_rupee
from app.seed.catalogs import COLOR_WEIGHT, SIZE_WEIGHT, BusinessSpec
from app.seed.reference import (
    DEV_PASSWORD,  # noqa: F401  (documented here for readers)
    FIRST_NAMES,
    LAST_NAMES,
    STATE_WEIGHTS,
    STATES,
    STREETS,
)
from app.seed.taxes import SHIPPING_SAC, quote_order, rates_on

IST = timedelta(hours=5, minutes=30)
RESTOCK_PERSONAS = {"BEST", "STEADY", "SEASONAL", "NEW", "DECLINING"}
HISTORY_DAYS = 90
OPENING_DAY = -91
HOUR_WEIGHTS = {
    8: 1,
    9: 2,
    10: 3,
    11: 5,
    12: 6,
    13: 6,
    14: 4,
    15: 3,
    16: 3,
    17: 4,
    18: 5,
    19: 7,
    20: 8,
    21: 8,
    22: 5,
    23: 2,
}


def q3(x) -> Decimal:
    return D(x).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def q4(x) -> Decimal:
    return D(x).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def fy_of(local_date: date, start_month: int = 4) -> str:
    year = local_date.year if local_date.month >= start_month else local_date.year - 1
    return f"{year}-{(year + 1) % 100:02d}"


def canonical_hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class TenantSim:
    def __init__(
        self,
        spec: BusinessSpec,
        business_id: str,
        owner_ids: dict,
        anchor: datetime,
        scale: float,
        seed: int,
        password_hash: str,
    ):
        self.spec, self.bid, self.owner_ids = spec, business_id, owner_ids
        self.now = anchor
        self.today = (anchor + IST).date()
        self.scale = scale
        self.r = random.Random(f"{seed}:{spec.key}")
        self.password_hash = password_hash
        self.rows: dict[str, list[dict]] = defaultdict(list)
        self.future: dict[int, list] = defaultdict(list)
        self.bal: dict[tuple[str, str], dict] = {}
        self.incoming: dict[str, Decimal] = defaultdict(Decimal)
        self.counters: dict[str, int] = defaultdict(int)
        self.pending_invoices: list[dict] = []
        self.pending_credit_notes: list[dict] = []
        self.receipts_by_po: dict[str, list[dict]] = defaultdict(list)
        self.buyers: list[dict] = []
        self.u_day = ZERO
        self.variants: list[dict] = []
        self.categories: dict[str, str] = {}
        self.warehouses: list[dict] = []
        self.suppliers: list[dict] = []
        self.customers: list[dict] = []
        self.by_variant: dict[str, list[dict]] = defaultdict(list)
        self._heap: list = []
        self._current_day: int | None = None

    # ------------------------------------------------------------------ helpers
    def uid(self) -> str:
        return str(uuid.UUID(int=self.r.getrandbits(128), version=4))

    def ts(self, day: int, hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
        local = datetime.combine(self.today + timedelta(days=day), time(hour, minute, second))
        return local - IST

    def local_date(self, moment: datetime) -> date:
        return (moment + IST).date()

    def seq(self, name: str) -> int:
        self.counters[name] += 1
        return self.counters[name]

    def poisson(self, lam: float) -> int:
        if lam <= 0:
            return 0
        if lam > 30:
            return max(0, int(round(self.r.gauss(lam, math.sqrt(lam)))))
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.r.random()
            if p <= limit:
                return k
            k += 1

    def add(self, table: str, **row) -> dict:
        self.rows[table].append(row)
        return row

    # ------------------------------------------------------------------ build
    def build(self) -> dict[str, list[dict]]:
        spec = self.spec
        self._people()
        if not spec.active:
            self._storefront_stub()
            return self.rows
        self._catalog()
        self._suppliers_and_warehouses()
        self._opening_stock()
        for day in range(OPENING_DAY + 1, 1):
            self._run_day(day)
        self._place_pending_orders()
        self._special_purchase_orders()
        self._cycle_counts_and_write_offs()
        self._finalize_balances()
        self._purchasing_documents()
        self._invoices_and_credit_notes()
        assert not any(d > 0 for d in self.future), "events scheduled in the future"
        self.rows["inventory_transactions"].sort(
            key=lambda row: row["created_at"]
        )  # stable: ids follow time
        self.rows["order_status_history"].sort(key=lambda row: row["created_at"])
        return self.rows

    # ------------------------------------------------------------------ people
    def _people(self) -> None:
        spec, r = self.spec, self.r
        self.customers: list[dict] = []
        others = [s for s in STATES if s != spec.state_code]
        other_w = [STATE_WEIGHTS[s] for s in others]
        n = int(spec.customers * max(self.scale, 0.15)) if spec.customers else 0
        for i in range(n):
            first, last = r.choice(FIRST_NAMES), r.choice(LAST_NAMES)
            if r.random() < spec.home_share:
                state = spec.state_code
            else:
                state = r.choices(others, weights=other_w)[0]
            city, prefix = r.choice(STATES[state][1])
            if state == spec.state_code and r.random() < 0.65:
                home = [c for c in STATES[state][1] if c[0] == spec.city]
                if home:
                    city, prefix = home[0]
            days_ago = int(150 * r.random() ** 1.4)
            created = self.ts(-days_ago, r.randint(8, 22), r.randint(0, 59))
            b2b = r.random() < 0.06
            name = f"{first} {last}" if not b2b else f"{last} Traders"
            customer = {
                "id": self.uid(),
                "name": name,
                "email": f"{first}.{last}{i}@example.com".lower(),
                "phone": f"+91 90000 {self.seq('phone'):05d}",
                "state": state,
                "city": city,
                "postal": f"{prefix}{r.randint(0, 999):03d}",
                "created": created,
                "gstin": with_checksum(
                    f"{state}AAC{last[0].upper()}{first[0].upper()}{r.randint(1000, 9999)}"
                    f"{r.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}1Z"
                )
                if b2b
                else None,
                "street": f"{r.randint(1, 240)}, {r.choice(STREETS)}",
            }
            self.customers.append(customer)
            self.add(
                "customers",
                id=customer["id"],
                business_id=self.bid,
                email=customer["email"],
                password_hash=self.password_hash,
                full_name=name,
                phone=customer["phone"],
                email_verified_at=created if r.random() < 0.7 else None,
                phone_verified_at=None,
                disabled_at=None,
                created_at=created,
                updated_at=created,
            )
            self.add(
                "customer_addresses",
                id=self.uid(),
                business_id=self.bid,
                customer_id=customer["id"],
                label="Home",
                recipient=name,
                phone=customer["phone"],
                line1=customer["street"],
                line2=None,
                city=city,
                state_code=state,
                postal_code=customer["postal"],
                is_default=True,
            )
            if r.random() < 0.25:
                s2 = r.choices(others, weights=other_w)[0]
                c2, p2 = r.choice(STATES[s2][1])
                self.add(
                    "customer_addresses",
                    id=self.uid(),
                    business_id=self.bid,
                    customer_id=customer["id"],
                    label="Work",
                    recipient=name,
                    phone=customer["phone"],
                    line1=f"{r.randint(1, 90)}, {r.choice(STREETS)}",
                    line2=None,
                    city=c2,
                    state_code=s2,
                    postal_code=f"{p2}{r.randint(0, 999):03d}",
                    is_default=False,
                )
        self.customers.sort(key=lambda c: c["created"])

    # ------------------------------------------------------------------ catalog
    def _catalog(self) -> None:
        spec, r = self.spec, self.r
        self.categories: dict[str, str] = {}
        slugs: set[str] = set()
        used_skus: set[str] = set()
        self.variants: list[dict] = []
        for position, p in enumerate(spec.products):
            if p.cat not in self.categories:
                cid = self.uid()
                self.categories[p.cat] = cid
                self.add(
                    "categories",
                    id=cid,
                    business_id=self.bid,
                    parent_id=None,
                    name=p.cat,
                    slug=slugify(p.cat),
                    position=len(self.categories),
                    archived_at=None,
                )
            slug = slugify(p.name)
            while slug in slugs:
                slug += "-2"
            slugs.add(slug)
            persona = p.persona
            status = {"DRAFT": "DRAFT", "ARCHIVED": "ARCHIVED"}.get(persona, "PUBLISHED")
            if status == "PUBLISHED" and spec.gst_registered:
                if p.tax not in ("GST_0",) and rates_on(p.tax, self.today) is None:
                    raise ValueError(
                        f"{p.name}: tax class {p.tax} has no rate in force on {self.today}"
                    )
            created_days = {"NEW": 12, "LAUNCH": 8}.get(persona, r.randint(100, 160))
            created = self.ts(-created_days, 10, r.randint(0, 59))
            product_id = self.uid()
            self.add(
                "products",
                id=product_id,
                business_id=self.bid,
                category_id=self.categories[p.cat],
                name=p.name,
                slug=slug,
                description=p.desc or f"{p.name} by {p.brand}.",
                brand=p.brand,
                status=status,
                hsn_sac=p.hsn,
                tax_class=p.tax,
                stock_unit=p.unit,
                track_inventory=True,
                created_at=created,
                updated_at=created,
                archived_at=self.ts(-30, 11) if persona == "ARCHIVED" else None,
            )
            combos: list[tuple[dict, float, Decimal]] = []
            if p.packs:
                for label, mult, w in p.packs:
                    combos.append(({"pack": label}, w, D(round(p.price * mult))))
            else:
                for size in p.sizes or [None]:
                    for color in p.colors or [None]:
                        opts = {k: v for k, v in (("size", size), ("color", color)) if v}
                        w = SIZE_WEIGHT.get(size, 1) * COLOR_WEIGHT.get(color, 1)
                        combos.append((opts, w, D(p.price)))
            share_total = (
                sum(w for _, w, _ in combos) or 1.0
            )  # variants split the product's popularity
            for vi, (opts, w, price) in enumerate(combos):
                w = w / share_total
                code = "-".join(self._option_code(v) for v in opts.values()) or "STD"
                sku = f"{spec.key[:3].upper()}-{position + 1:03d}-{code}"
                suffix = 2
                while sku in used_skus:  # SKUs are unique per business
                    sku, suffix = f"{sku.split('~')[0]}~{suffix}", suffix + 1
                used_skus.add(sku)
                mrp = D(round(float(price) * r.choice([1, 1, 1.1, 1.2, 1.25])))
                cost = q4(price * D(str(p.cost)))
                v = {
                    "id": self.uid(),
                    "product_id": product_id,
                    "p": p,
                    "status": status,
                    "opts": opts,
                    "pw": w,
                    "price": price,
                    "mrp": mrp,
                    "cost": cost,
                    "sku": sku,
                    "persona": persona,
                    "created_day": -created_days,
                    "stockout_day": r.randint(-24, -3),
                    "slot": vi,
                    "name": p.name,
                }
                self.variants.append(v)
        self._calibrate()
        for v in self.variants:
            lead = self._supplier_lead(v)
            thr = math.ceil(float(v["lam"]) * (lead + 4))
            floor = 2 if v["p"].unit in ("PCS", "PAIR", "BOX") else 3
            v["thr"] = D(max(floor, thr))
            options_text = json.dumps(v["opts"], sort_keys=True)
            created = self.ts(v["created_day"], 10)
            self.add(
                "product_variants",
                id=v["id"],
                business_id=self.bid,
                product_id=v["product_id"],
                sku=v["sku"],
                options=v["opts"],
                option_hash=hashlib.sha256(options_text.encode()).hexdigest(),
                barcode=f"89{self.r.randint(10**10, 10**11 - 1)}",
                cost_price=v["cost"],
                selling_price=v["price"],
                mrp=v["mrp"],
                reorder_threshold=v["thr"],
                is_active=True,
                created_at=created,
                updated_at=created,
                archived_at=None,
            )

    @staticmethod
    def _option_code(value) -> str:
        words = re.sub(r"[^A-Za-z0-9 ]", "", str(value)).upper().split()
        if any(ch.isdigit() for w in words for ch in w):  # pack sizes: keep "500 ml" -> 500ML
            return "".join(words)[:8]
        if len(words) == 1:
            return words[0][:4]
        return "".join(w[:2] for w in words[:3])

    def _supplier_lead(self, v) -> int:
        suppliers = self.spec.suppliers
        return suppliers[v["p"].sup % len(suppliers)][1] if suppliers else 7

    def weight(self, v: dict, day: int) -> float:
        base = {
            "BEST": 10,
            "STEADY": 4,
            "SLOW": 0.8,
            "LOW": 4,
            "STOCKOUT": 5,
            "SEASONAL": 2,
            "DECLINING": 4,
            "NEW": 6,
            "LAUNCH": 15,
            "DEAD": 3,
            "ARCHIVED": 4,
        }.get(v["persona"], 0)
        persona = v["persona"]
        if persona == "DEAD" and day >= -65:
            return 0.0
        if persona == "NEW" and day < -12:
            return 0.0
        if persona == "LAUNCH" and day < -8:
            return 0.0
        if persona == "ARCHIVED" and day >= -40:
            return 0.0
        if persona == "SEASONAL":
            base *= 1 + 4 * max(0.0, min(1.0, (day + 30) / 30))
        if persona == "DECLINING":
            base *= 1.5 - 1.3 * ((day + HISTORY_DAYS) / HISTORY_DAYS)
        if v["status"] != "PUBLISHED" and persona != "ARCHIVED":
            return 0.0
        if v["created_day"] > day:
            return 0.0
        return base * v["pw"] * (self.spec_demand_factor(v["p"]))

    @staticmethod
    def spec_demand_factor(p) -> float:
        # cheaper items are bought more often than expensive ones
        return (
            1.0 if p.price <= 1000 else 0.8 if p.price <= 3000 else 0.5 if p.price <= 10000 else 0.3
        )

    def _calibrate(self) -> None:
        spec = self.spec
        n0 = spec.orders_per_day * self.scale
        self.u_day = D(n0 * (1 + spec.growth / 2) * spec.lines_mean * 1.45)
        avg = {}
        for v in self.variants:
            avg[v["id"]] = sum(self.weight(v, d) for d in range(-HISTORY_DAYS, 0)) / HISTORY_DAYS
        total = sum(avg.values()) or 1
        for v in self.variants:
            v["avgw"] = avg[v["id"]]
            v["lam"] = self.u_day * D(avg[v["id"]] / total)
        self._peak_total = {}

    # ------------------------------------------------------------------ suppliers, warehouses
    def _suppliers_and_warehouses(self) -> None:
        spec, r = self.spec, self.r
        self.warehouses: list[dict] = []
        for i, (code, name, city) in enumerate(spec.warehouses):
            w = {"id": self.uid(), "code": code}
            self.warehouses.append(w)
            self.add(
                "warehouses",
                id=w["id"],
                business_id=self.bid,
                code=code,
                name=name,
                address=f"{name}, {city}",
                state_code=spec.state_code,
                is_default=(i == 0),
                archived_at=None,
            )
        self.suppliers: list[dict] = []
        for i, (name, lead, terms) in enumerate(spec.suppliers):
            state = r.choice(["27", "29", "33", "07", "36", "24"])
            letters = [c.upper() for c in name if c.isalpha()]
            pan = "AA" + "".join(letters[:2]) + letters[-1]
            gstin = (
                None
                if i == len(spec.suppliers) - 1 and terms == 0
                else with_checksum(
                    f"{state}{pan}{r.randint(1000, 9999)}{r.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}1Z"
                )
            )
            s = {"id": self.uid(), "name": name, "lead": lead, "terms": terms, "gstin": gstin}
            self.suppliers.append(s)
            self.add(
                "suppliers",
                id=s["id"],
                business_id=self.bid,
                name=name,
                contact_name=r.choice(FIRST_NAMES) + " " + r.choice(LAST_NAMES),
                email=f"sales@{slugify(name)}.example",
                phone=f"+91 98000 {r.randint(10000, 99999)}",
                gstin=gstin,
                address=f"{name}, India",
                lead_time_days=lead,
                payment_terms_days=terms,
                archived_at=None,
                created_at=self.ts(-170, 10),
            )

    def _supplier(self, v) -> dict:
        return self.suppliers[v["p"].sup % len(self.suppliers)]

    # ------------------------------------------------------------------ ledger and balances
    def _balance(self, v_id: str, wh_id: str) -> dict:
        b = self.bal.get((v_id, wh_id))
        if b is None:
            b = {"on_hand": ZERO, "reserved": ZERO, "avg": ZERO, "touched": False}
            self.bal[(v_id, wh_id)] = b
            self.by_variant[v_id].append(b)
        return b

    def _avail(self, v: dict) -> Decimal:
        return sum((b["on_hand"] - b["reserved"] for b in self.by_variant[v["id"]]), ZERO)

    def _ledger(
        self,
        when: datetime,
        v_id: str,
        wh_id: str,
        kind: str,
        delta,
        *,
        cost=None,
        reason: str,
        source: str,
        source_id: str | None,
        key: str,
        actor: str = "SYSTEM",
        actor_id=None,
    ) -> None:
        delta = q3(delta)
        b = self._balance(v_id, wh_id)
        if kind in ("RECEIPT", "OPENING") and cost is not None:
            total = b["on_hand"] + delta
            b["avg"] = (
                q4(((b["on_hand"] * b["avg"]) + (delta * D(cost))) / total) if total else D(cost)
            )
        b["on_hand"] += delta
        b["touched"] = True
        if b["on_hand"] < b["reserved"] or b["on_hand"] < 0:
            raise ValueError(f"ledger would break balance rules for {v_id}")
        unit_cost = q4(cost) if cost is not None else q4(b["avg"])
        self.add(
            "inventory_transactions",
            business_id=self.bid,
            variant_id=v_id,
            warehouse_id=wh_id,
            txn_type=kind,
            quantity_delta=delta,
            unit_cost=unit_cost,
            reason_code=reason,
            note=None,
            source_type=source,
            source_id=source_id,
            idempotency_key=key,
            actor_type=actor,
            actor_id=actor_id,
            created_at=when,
        )

    def _opening_stock(self) -> None:
        when = self.ts(OPENING_DAY, 9)
        main = self.warehouses[0]["id"]
        store = self.warehouses[1]["id"] if len(self.warehouses) > 1 else None
        for v in self.variants:
            lam = float(v["lam"])
            persona = v["persona"]
            cover = {
                "BEST": 45,
                "STEADY": 50,
                "SLOW": 280,
                "LOW": 70,
                "SEASONAL": 40,
                "DECLINING": 60,
                "NEW": 40,
                "ARCHIVED": 30,
            }.get(persona)
            if persona == "STOCKOUT":
                qty = lam * (HISTORY_DAYS + v["stockout_day"]) * 0.95
            elif persona == "DEAD":
                qty = (
                    float(self.u_day)
                    * (3 * v["pw"] * self.spec_demand_factor(v["p"]) / max(self._sum_peak(), 1))
                    * 26
                    * 2.2
                )
            elif persona == "LAUNCH":
                qty = lam * 6
            elif persona == "DRAFT":
                qty = 15
            elif persona == "NEW":
                qty = lam * 40
            else:
                qty = lam * cover
            if v["p"].unit not in ("PCS", "PAIR", "BOX"):
                qty = round(qty)
            thr = int(v["thr"])
            minimum = {
                "BEST": thr * 3,
                "STEADY": thr * 3,
                "SEASONAL": thr * 3,
                "NEW": thr * 3,
                "DECLINING": thr * 3,
                "SLOW": max(12, thr * 4),
                "DEAD": 8,
                "ARCHIVED": 6,
                "LOW": thr * 2,
                "STOCKOUT": 6,
                "LAUNCH": 6,
                "DRAFT": 15,
            }.get(persona, 6)
            qty = max(int(qty), minimum)
            if persona in ("NEW", "LAUNCH"):
                when_v = self.ts(v["created_day"], 11)
            else:
                when_v = when
            share_store = store and persona in ("BEST", "STEADY", "SEASONAL", "NEW") and qty >= 8
            parts = [(main, qty - (qty // 4 if share_store else 0))]
            if share_store:
                parts.append((store, qty // 4))
            if persona in ("NEW", "LAUNCH"):
                self.future[v["created_day"]].append(
                    (when_v, self.seq("ev"), "opening", {"v": v, "parts": parts})
                )  # before the day loop
                continue
            for wh, q in parts:
                if q > 0:
                    self._ledger(
                        when_v,
                        v["id"],
                        wh,
                        "OPENING",
                        q,
                        cost=v["cost"],
                        reason="OPENING_STOCK",
                        source="OPENING",
                        source_id=None,
                        key=f"open:{v['id']}:{wh}",
                    )

    def _ev_opening(self, when, data, day, weights):
        v = data["v"]
        for wh, q in data["parts"]:
            if q > 0:
                self._ledger(
                    when,
                    v["id"],
                    wh,
                    "OPENING",
                    q,
                    cost=v["cost"],
                    reason="OPENING_STOCK",
                    source="OPENING",
                    source_id=None,
                    key=f"open:{v['id']}:{wh}",
                )

    def _sum_peak(self) -> float:
        if not hasattr(self, "_peak_cache"):
            self._peak_cache = sum(self.weight(v, -80) for v in self.variants) or 1.0
        return self._peak_cache

    # ------------------------------------------------------------------ days
    def _run_day(self, day: int) -> None:
        events = self.future.pop(day, [])
        if day >= -HISTORY_DAYS:
            events += self._new_order_events(day)
            events += self._maintenance_events(day)
        heapq.heapify(events)
        self._heap, self._current_day = events, day
        weights = None
        if day >= -HISTORY_DAYS:
            ws = [self.weight(v, day) for v in self.variants]
            cum, acc = [], 0.0
            for w in ws:
                acc += w
                cum.append(acc)
            weights = (cum, acc)
        while events:
            when, _n, kind, data = heapq.heappop(events)
            getattr(self, f"_ev_{kind}")(when, data, day, weights)
        self._current_day = None
        if day >= -HISTORY_DAYS:
            self._reorder_check(day)

    def _schedule(self, day: int, event: tuple) -> None:
        """Queue an event; same-day events join the heap currently being processed."""
        if day == self._current_day:
            heapq.heappush(self._heap, event)
        else:
            self.future[day].append(event)

    def _new_order_events(self, day: int) -> list:
        spec = self.spec
        local = self.today + timedelta(days=day)
        weekday = local.weekday()
        factor = 1.35 if weekday >= 5 else 1.1 if weekday == 4 else 1.0
        trend = 1 + spec.growth * ((day + HISTORY_DAYS) / HISTORY_DAYS)
        event = 2.2 if -12 <= day <= -8 else 1.0
        mean = spec.orders_per_day * self.scale * factor * trend * event
        limit = self.now - timedelta(minutes=25)
        if day == 0:
            elapsed = ((self.now + IST).hour * 60 + (self.now + IST).minute) / 1440
            mean *= elapsed
        out = []
        hours = list(HOUR_WEIGHTS)
        hw = list(HOUR_WEIGHTS.values())
        for _ in range(self.poisson(mean)):
            hour = self.r.choices(hours, weights=hw)[0]
            when = self.ts(day, hour, self.r.randint(0, 59), self.r.randint(0, 59))
            if when > limit:
                continue
            out.append((when, self.seq("ev"), "order", {}))
        return out

    # periodic non-order stock events (damage, cycle counts, transfers)
    def _maintenance_events(self, day: int) -> list:
        out = []
        if self.r.random() < 0.12:
            out.append((self.ts(day, 15, 30), self.seq("ev"), "damage", {}))
        if len(self.warehouses) > 1 and ((day % 7 == 0 and day < -1) or day == -1):
            out.append((self.ts(day, 10, 15), self.seq("ev"), "transfer", {"final": day == -1}))
        if self.r.random() < 0.05:
            out.append((self.ts(day, 17), self.seq("ev"), "count", {}))
        return out

    # ------------------------------------------------------------------ events
    def _ev_order(self, when, data, day, weights):
        self._place_order(when, day, weights)

    def _ev_commit(self, when, data, day, weights):
        res = data["res"]
        res["status"], res["updated_at"] = "COMMITTED", when
        v_id, wh = res["variant_id"], res["warehouse_id"]
        b = self._balance(v_id, wh)
        b["reserved"] -= res["quantity"]
        self._ledger(
            when,
            v_id,
            wh,
            "SALE",
            -res["quantity"],
            reason="ORDER_PAID",
            source="ORDER",
            source_id=res["order_id"],
            key=f"sale:{res['id']}",
        )

    def _ev_release(self, when, data, day, weights):
        res = data["res"]
        res["status"], res["updated_at"] = data["status"], when
        self._balance(res["variant_id"], res["warehouse_id"])["reserved"] -= res["quantity"]

    def _ev_restock_cancel(self, when, data, day, weights):
        res = data["res"]
        self._ledger(
            when,
            res["variant_id"],
            res["warehouse_id"],
            "CUSTOMER_RETURN",
            res["quantity"],
            reason="ORDER_CANCELLED",
            source="ORDER",
            source_id=res["order_id"],
            key=f"cancel:{res['id']}",
        )

    def _ev_return_receive(self, when, data, day, weights):
        for item in data["items"]:
            res = item["res"]
            self._ledger(
                when,
                res["variant_id"],
                res["warehouse_id"],
                "CUSTOMER_RETURN",
                item["qty"],
                reason="CUSTOMER_RETURN",
                source="RETURN",
                source_id=data["return_id"],
                key=f"ret:{data['return_id']}:{item['row_id']}",
            )
            if item["condition"] == "DAMAGED":
                self._ledger(
                    when,
                    res["variant_id"],
                    res["warehouse_id"],
                    "DAMAGE",
                    -item["qty"],
                    reason="RETURN_DAMAGED",
                    source="RETURN",
                    source_id=data["return_id"],
                    key=f"retdmg:{data['return_id']}:{item['row_id']}",
                )

    def _ev_receipt(self, when, data, day, weights):
        po, lines, final = data["po"], data["lines"], data["final"]
        grn = self.seq("grn")
        receipt_id = self.uid()
        main = self.warehouses[0]["id"]
        receipt = self.add(
            "goods_receipts",
            id=receipt_id,
            business_id=self.bid,
            purchase_order_id=po["id"],
            supplier_id=po["supplier_id"],
            warehouse_id=main,
            number=f"GRN-{grn:05d}",
            received_at=when,
            status="POSTED",
            idempotency_key=f"grn:{receipt_id}",
            created_by=self.owner_ids["manager"],
            created_at=when,
        )
        self.receipts_by_po[po["id"]].append(receipt)
        for item, qty in lines:
            self.add(
                "goods_receipt_items",
                id=self.uid(),
                receipt_id=receipt_id,
                business_id=self.bid,
                variant_id=item["variant_id"],
                quantity=qty,
                unit_cost=item["unit_cost"],
            )
            self._ledger(
                when,
                item["variant_id"],
                main,
                "RECEIPT",
                qty,
                cost=item["unit_cost"],
                reason="GOODS_RECEIPT",
                source="GOODS_RECEIPT",
                source_id=receipt_id,
                key=f"rcpt:{receipt_id}:{item['id']}",
                actor="OWNER",
                actor_id=self.owner_ids["manager"],
            )
            item["received_quantity"] = D(item["received_quantity"]) + qty
            self.incoming[item["variant_id"]] -= qty
        po["status"] = "RECEIVED" if final else "PARTIALLY_RECEIVED"
        po["updated_at"] = when

    def _ev_damage(self, when, data, day, weights):
        candidates = [
            v for v in self.variants if v["status"] == "PUBLISHED" and self._avail(v) >= 6
        ]
        if not candidates:
            return
        v = self.r.choice(candidates)
        qty = D(self.r.randint(1, 2))
        wh = self._wh_with_stock(v, qty)
        if not wh:
            return
        reason = self.r.choice(["TRANSIT_DAMAGE", "DAMAGED_IN_STORAGE", "QUALITY_REJECT"])
        self._post_adjustment(when, reason, [(v, wh, -qty)], kind="DAMAGE")

    def _ev_count(self, when, data, day, weights):
        candidates = [
            v for v in self.variants if v["status"] == "PUBLISHED" and self._avail(v) >= 6
        ]
        if not candidates:
            return
        v = self.r.choice(candidates)
        delta = D(self.r.choice([-2, -1, 1, 2, 3]))
        wh = self._wh_with_stock(v, abs(delta))
        if not wh:
            return
        self._post_adjustment(when, "CYCLE_COUNT", [(v, wh, delta)], kind="ADJUSTMENT")

    def _ev_transfer(self, when, data, day, weights):
        main, store = self.warehouses[0]["id"], self.warehouses[1]["id"]
        lines = []
        for v in self.variants:
            if v["persona"] in ("BEST", "STEADY") and v["status"] == "PUBLISHED":
                qty = D(max(2, round(float(v["lam"]) * 6)))
                if (
                    self._balance(v["id"], main)["on_hand"]
                    - self._balance(v["id"], main)["reserved"]
                    >= qty + v["thr"]
                ):
                    lines.append((v, qty))
        if not lines:
            return
        lines = lines[:8]
        tid = self.uid()
        in_transit = data["final"]
        self.add(
            "stock_transfers",
            id=tid,
            business_id=self.bid,
            from_warehouse_id=main,
            to_warehouse_id=store,
            status="IN_TRANSIT" if in_transit else "RECEIVED",
            created_at=when,
            dispatched_at=when,
            received_at=None if in_transit else when + timedelta(hours=20),
        )
        for v, qty in lines:
            self.add(
                "stock_transfer_lines",
                id=self.uid(),
                transfer_id=tid,
                business_id=self.bid,
                variant_id=v["id"],
                quantity=qty,
            )
            self._ledger(
                when,
                v["id"],
                main,
                "TRANSFER_OUT",
                -qty,
                reason="TRANSFER",
                source="TRANSFER",
                source_id=tid,
                key=f"tout:{tid}:{v['id']}",
                actor="OWNER",
                actor_id=self.owner_ids["manager"],
            )
            if not in_transit:
                self._schedule(
                    day + 1,
                    (
                        when + timedelta(hours=20),
                        self.seq("ev"),
                        "transfer_in",
                        {"tid": tid, "v": v, "qty": qty, "wh": store, "src": main},
                    ),
                )

    def _ev_transfer_in(self, when, data, day, weights):
        v, tid = data["v"], data["tid"]
        cost = self._balance(v["id"], data["src"])["avg"] or v["cost"]
        self._ledger(
            when,
            v["id"],
            data["wh"],
            "TRANSFER_IN",
            data["qty"],
            cost=cost,
            reason="TRANSFER",
            source="TRANSFER",
            source_id=tid,
            key=f"tin:{tid}:{v['id']}",
            actor="OWNER",
            actor_id=self.owner_ids["manager"],
        )

    def _post_adjustment(self, when, reason, lines, *, kind) -> None:
        aid = self.uid()
        self.add(
            "stock_adjustments",
            id=aid,
            business_id=self.bid,
            reason_code=reason,
            note=None,
            status="POSTED",
            created_by=self.owner_ids["manager"],
            approved_by=self.owner_ids["owner"],
            posted_at=when,
            created_at=when - timedelta(minutes=30),
        )
        for v, wh, delta in lines:
            on_hand = self._balance(v["id"], wh)["on_hand"]
            self.add(
                "stock_adjustment_lines",
                id=self.uid(),
                adjustment_id=aid,
                business_id=self.bid,
                variant_id=v["id"],
                warehouse_id=wh,
                counted_qty=on_hand + delta,
                delta_qty=delta,
            )
            self._ledger(
                when,
                v["id"],
                wh,
                kind,
                delta,
                reason=reason,
                source="ADJUSTMENT",
                source_id=aid,
                key=f"adj:{aid}:{v['id']}",
                actor="OWNER",
                actor_id=self.owner_ids["manager"],
            )
        self.add(
            "audit_events",
            service="supply",
            business_id=self.bid,
            actor_type="OWNER",
            actor_id=self.owner_ids["manager"],
            action="stock_adjustment.posted",
            entity_type="stock_adjustment",
            entity_id=aid,
            before=None,
            after={"reason": reason, "lines": len(lines)},
            request_id=None,
            created_at=when,
        )

    def _wh_with_stock(self, v, qty: Decimal, prefer: str | None = None) -> str | None:
        order = list(self.warehouses)
        if prefer == "STORE" and len(order) > 1:
            order = order[::-1]
        for w in order:
            b = self.bal.get((v["id"], w["id"]))
            if b is not None and b["on_hand"] - b["reserved"] >= qty:
                return w["id"]
        return None

    # ------------------------------------------------------------------ restocking
    def _reorder_check(self, day: int) -> None:
        by_supplier: dict[str, list] = defaultdict(list)
        total_w = sum(self.weight(v, day) for v in self.variants) or 1
        for v in self.variants:
            if (
                v["persona"] not in RESTOCK_PERSONAS
                or v["status"] != "PUBLISHED"
                or v["created_day"] > day
            ):
                continue
            lam = float(self.u_day) * self.weight(v, day) / total_w
            if lam <= 0:
                continue
            lead = self._supplier(v)["lead"]
            position = self._avail(v) + self.incoming[v["id"]]
            # reorder at twice the stock threshold (or the lead-time demand), order up to >= 4x threshold
            if float(position) <= max(float(v["thr"]) * 2, lam * (lead + 4)):
                need = max(lam * 35, float(v["thr"]) * 4) - float(position)
                step = 5 if v["p"].unit in ("KG", "L") else 1
                qty = max(step, int(math.ceil(need / step) * step))
                by_supplier[self._supplier(v)["id"]].append((v, D(qty)))
        for sup_id, lines in by_supplier.items():
            sup = next(s for s in self.suppliers if s["id"] == sup_id)
            self._create_po(day, sup, lines)

    def _create_po(
        self,
        day: int,
        sup: dict,
        lines: list,
        *,
        delay: int | None = None,
        status="APPROVED",
        receive: bool = True,
    ) -> dict:
        number = f"PO-{self.seq('po'):05d}"
        created = self.ts(day, 18, self.r.randint(0, 59))
        jitter = (
            delay
            if delay is not None
            else (self.r.randint(2, 5) if self.r.random() < 0.15 else self.r.randint(0, 2))
        )
        expected_day = day + sup["lead"] + jitter
        total = ZERO
        po = self.add(
            "purchase_orders",
            id=self.uid(),
            business_id=self.bid,
            supplier_id=sup["id"],
            number=number,
            status=status,
            expected_date=self.today + timedelta(days=expected_day),
            total=ZERO,
            created_by=self.owner_ids["manager"],
            created_at=created,
            updated_at=created,
        )
        items = []
        for v, qty in lines:
            unit_cost = q4(v["cost"] * D(str(self.r.uniform(0.97, 1.03))))
            item = self.add(
                "purchase_order_items",
                id=self.uid(),
                purchase_order_id=po["id"],
                business_id=self.bid,
                variant_id=v["id"],
                quantity=qty,
                unit_cost=unit_cost,
                received_quantity=ZERO,
            )
            items.append(item)
            total += round2(qty * unit_cost)
            if status == "APPROVED":
                self.incoming[v["id"]] += qty
        po["total"] = total
        if status == "APPROVED" and receive and expected_day <= 0:
            partial = self.r.random() < 0.12 and all(it["quantity"] >= 2 for it in items)
            first = [(it, self._split(it)[0] if partial else it["quantity"]) for it in items]
            rec_day = min(expected_day, 0)
            when = self.ts(rec_day, self.r.randint(11, 17), self.r.randint(0, 59))
            if when <= self.now - timedelta(minutes=30):
                self._schedule(
                    rec_day,
                    (
                        when,
                        self.seq("ev"),
                        "receipt",
                        {"po": po, "lines": first, "final": not partial},
                    ),
                )
                if partial and rec_day + 3 <= 0:
                    rest = [(it, self._split(it)[1]) for it in items]
                    self._schedule(
                        rec_day + 3,
                        (
                            self.ts(rec_day + 3, 12),
                            self.seq("ev"),
                            "receipt",
                            {"po": po, "lines": rest, "final": True},
                        ),
                    )
        return po

    @staticmethod
    def _split(item) -> tuple[Decimal, Decimal]:
        """Whole-unit 70/30 split of an ordered quantity for partial deliveries."""
        qty = item["quantity"]
        first = max(D(1), (qty * D("0.7")).to_integral_value(rounding="ROUND_FLOOR"))
        return first, qty - first

    def _special_purchase_orders(self) -> None:
        """Late/overdue and in-transit purchase orders that explain today's stock-outs and low stock."""
        r = self.r
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for v in self.variants:
            if v["persona"] in ("STOCKOUT", "LOW", "LAUNCH") and v["status"] == "PUBLISHED":
                qty = D(max(10, round(float(v["lam"]) * 30)))
                if v["p"].unit in ("KG", "L"):
                    qty = D(max(10, int(qty) // 5 * 5))
                groups[(v["persona"], self._supplier(v)["id"])].append((v, qty))
        for (persona, sup_id), lines in groups.items():
            sup = next(s for s in self.suppliers if s["id"] == sup_id)
            if persona == "STOCKOUT":
                overdue = -r.randint(2, 9)
                po = self._create_po(overdue - sup["lead"], sup, lines, delay=0, receive=False)
                po["expected_date"] = self.today + timedelta(days=overdue)
            else:  # in transit, arriving in a few days
                po = self._create_po(-max(2, sup["lead"] - 3), sup, lines, delay=0, receive=False)
                po["expected_date"] = self.today + timedelta(days=r.randint(1, 4))
        # a draft and a cancelled PO per business for state variety
        if self.suppliers and self.variants:
            sample = [(v, D(20)) for v in self.variants if v["status"] == "PUBLISHED"][:3]
            self._create_po(-3, self.suppliers[0], sample, status="DRAFT", receive=False)
            sample2 = [(v, D(15)) for v in self.variants if v["status"] == "PUBLISHED"][3:5]
            if sample2:
                self._create_po(-40, self.suppliers[-1], sample2, status="CANCELLED", receive=False)

    # ------------------------------------------------------------------ orders
    def _pick_customer(self, when: datetime) -> dict | None:
        eligible = [c for c in self.customers if c["created"] <= when - timedelta(minutes=10)]
        if not eligible:
            return None
        if self.buyers and self.r.random() < 0.35:
            return self.r.choice(self.buyers)
        recent = eligible[-max(10, len(eligible) // 2) :] if self.r.random() < 0.5 else eligible
        return self.r.choice(recent)

    def _qty_for(self, unit: str) -> Decimal:
        r = self.r
        if unit in ("KG", "L"):
            return D(r.choice([0.5, 1, 1, 1, 1.5, 2, 2, 3]))
        if unit in ("DOZEN", "PACK"):
            return D(r.choice([1, 1, 1, 2, 2, 3]))
        return D(r.choices([1, 2, 3], weights=[76, 18, 6])[0])

    def _place_order(
        self, when: datetime, day: int, weights, *, forced_pending: bool = False
    ) -> None:
        r, spec = self.r, self.spec
        customer = self._pick_customer(when)
        if customer is None:
            return
        cum, total = weights if weights else ([], 0)
        if forced_pending:
            cum, acc = [], 0.0
            for v in self.variants:
                acc += self.weight(v, 0)
                cum.append(acc)
            total = acc
        if total <= 0:
            return
        k = r.choices([1, 2, 3, 4, 5, 6], weights=self._line_weights())[0]
        chosen: list[tuple[dict, Decimal, str]] = []
        used: set[str] = set()
        home_city = customer["city"] == spec.city
        for _ in range(k * 4):
            if len(chosen) >= k:
                break
            v = self.variants[self._bisect(cum, r.random() * total)]
            if v["id"] in used:
                continue
            qty = self._qty_for(v["p"].unit)
            wh = self._wh_with_stock(v, qty, prefer="STORE" if home_city else None)
            if wh is None:
                continue
            used.add(v["id"])
            chosen.append((v, qty, wh))
        if not chosen:
            return
        method = "ONLINE" if forced_pending or r.random() >= spec.cod_share else "COD"
        plan = self._lifecycle(when, method, forced_pending)
        discount = D(r.choice([0, 0, 0, 0, 0, 0, 5, 10, 15, 10]))
        inter = customer["state"] != spec.state_code
        quote = quote_order(
            [(q, v["price"], v["p"].tax) for v, q, _ in chosen],
            seller_registered=spec.gst_registered,
            inter_state=inter,
            include_tax=spec.prices_include_tax,
            discount_pct=discount,
            shipping_gross=ZERO,
            on=self.local_date(when),
        )
        net_goods = quote.subtotal - quote.discount_total
        ship_fee = ZERO if net_goods >= spec.free_shipping_over else spec.flat_shipping
        if ship_fee:
            quote = quote_order(
                [(q, v["price"], v["p"].tax) for v, q, _ in chosen],
                seller_registered=spec.gst_registered,
                inter_state=inter,
                include_tax=spec.prices_include_tax,
                discount_pct=discount,
                shipping_gross=ship_fee,
                on=self.local_date(when),
            )
        oid, number = (
            self.uid(),
            f"{spec.invoice_prefix}-{self.local_date(when):%y%m}-{self.seq('order'):05d}",
        )
        key = f"seed-{oid}"
        ship_to = {
            "recipient": customer["name"],
            "phone": customer["phone"],
            "line1": customer["street"],
            "city": customer["city"],
            "state_code": customer["state"],
            "postal_code": customer["postal"],
        }
        if customer["gstin"]:
            ship_to["gstin"] = customer["gstin"]
        status = plan["order"]
        order = self.add(
            "orders",
            id=oid,
            business_id=self.bid,
            customer_id=customer["id"],
            order_number=number,
            order_status=status,
            payment_status=plan["payment"],
            fulfilment_status=plan["fulfilment"],
            payment_method=method,
            currency="INR",
            subtotal=quote.subtotal,
            discount_total=quote.discount_total,
            tax_total=quote.tax_total,
            shipping_total=quote.shipping_total,
            round_off=quote.round_off,
            grand_total=quote.grand_total,
            ship_to=ship_to,
            place_of_supply_state=customer["state"],
            idempotency_key=key,
            placed_at=when,
            cancelled_at=plan.get("t_cancel") or plan.get("t_fail"),
            cancel_reason=plan.get("cancel_reason"),
            created_at=when,
            updated_at=plan["last"],
        )
        self.buyers.append(customer)
        order_items = []
        reservations = []
        for idx, ((v, qty, wh), line) in enumerate(zip(chosen, quote.lines)):
            oi = self.add(
                "order_items",
                id=self.uid(),
                business_id=self.bid,
                order_id=oid,
                product_id=v["product_id"],
                variant_id=v["id"],
                product_name=v["name"],
                sku=v["sku"],
                options=v["opts"],
                hsn_sac=v["p"].hsn,
                stock_unit=v["p"].unit,
                quantity=qty,
                unit_price=v["price"],
                discount=line.discount,
                taxable_value=line.taxable,
                cgst=line.cgst,
                sgst=line.sgst,
                igst=line.igst,
                cess=line.cess,
                tax_class=v["p"].tax,
                tax_rate_version_id=line.version_id,
                line_total=line.line_total,
            )
            order_items.append((oi, v, qty, line))
            b = self._balance(v["id"], wh)
            b["reserved"] += qty
            expires = when + timedelta(
                hours=24 if (method == "COD" or plan["order"] == "PENDING_PAYMENT") else 0,
                minutes=15,
            )
            res = self.add(
                "stock_reservations",
                id=self.uid(),
                business_id=self.bid,
                order_id=oid,
                variant_id=v["id"],
                warehouse_id=wh,
                quantity=qty,
                status="ACTIVE",
                expires_at=expires,
                idempotency_key=f"{key}:{idx}",
                created_at=when,
                updated_at=when,
            )
            reservations.append(res)
            oi["_res"] = res
        order["_quote"], order["_items"], order["_customer"], order["_plan"] = (
            quote,
            order_items,
            customer,
            plan,
        )
        self._history(order, plan, when)
        self._payment_and_stock(order, plan, reservations, method, day)
        if plan.get("t_ship"):
            self.pending_invoices.append(order)
        if plan.get("returns"):
            self._returns(order, plan, day)

    def _line_weights(self) -> list[float]:
        mean = self.spec.lines_mean
        return [max(0.01, math.exp(-abs(i - mean) * 1.1)) for i in range(1, 7)]

    @staticmethod
    def _bisect(cum: list[float], x: float) -> int:
        lo, hi = 0, len(cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid] < x:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _lifecycle(self, placed: datetime, method: str, forced_pending: bool) -> dict:
        r = self.r
        lim = self.now - timedelta(minutes=10)
        plan: dict = {
            "order": "CONFIRMED",
            "payment": "UNPAID",
            "fulfilment": "UNFULFILLED",
            "last": placed,
        }
        if forced_pending:
            plan.update(order="PENDING_PAYMENT", payment="PENDING", last=placed)
            return plan
        age = self.now - placed
        if method == "ONLINE" and r.random() < 0.03 and age > timedelta(minutes=30):
            t_fail = placed + timedelta(minutes=r.uniform(2, 14))
            plan.update(
                order="CANCELLED",
                payment="FAILED",
                t_fail=t_fail,
                cancel_reason="Payment failed",
                last=t_fail,
            )
            return plan
        t_paid = min(
            lim,
            placed
            + (
                timedelta(seconds=r.uniform(30, 180))
                if method == "ONLINE"
                else timedelta(minutes=r.uniform(1, 20))
            ),
        )
        t_pack = t_paid + timedelta(hours=r.uniform(3, 20))
        t_ship = t_pack + timedelta(hours=r.uniform(8, 36))
        t_del = t_ship + timedelta(hours=r.uniform(30, 100))
        plan["t_paid"] = t_paid
        plan["payment"] = "PAID" if method == "ONLINE" else "UNPAID"
        if r.random() < 0.045 and age > timedelta(hours=3):
            t_cancel = t_paid + timedelta(hours=r.uniform(0.3, 2.5))
            if t_cancel < t_pack and t_cancel <= lim:
                plan.update(
                    order="CANCELLED",
                    t_cancel=t_cancel,
                    cancel_reason="Cancelled by customer",
                    last=t_cancel,
                    payment="REFUNDED" if method == "ONLINE" else "UNPAID",
                    t_refund=t_cancel + timedelta(minutes=r.uniform(10, 120))
                    if method == "ONLINE"
                    else None,
                )
                if plan.get("t_refund") and plan["t_refund"] > lim:
                    plan["t_refund"], plan["payment"] = None, "PAID"
                return plan
        plan["last"] = t_paid
        if t_pack <= lim:
            plan.update(order="PACKED", fulfilment="PACKED", t_pack=t_pack, last=t_pack)
        if t_ship <= lim:
            plan.update(order="SHIPPED", fulfilment="SHIPPED", t_ship=t_ship, last=t_ship)
        if t_del <= lim:
            plan.update(order="DELIVERED", fulfilment="DELIVERED", t_deliver=t_del, last=t_del)
            if method == "COD":
                plan["payment"] = "PAID"
            p_return = {
                "fashion": 0.09,
                "electronics": 0.08,
                "gaming": 0.06,
                "grocery": 0.02,
                "food": 0.03,
            }[self.spec.template]
            if r.random() < p_return:
                t_req = t_del + timedelta(days=r.uniform(0.5, 4))
                if t_req <= lim:
                    plan["returns"] = {"t_req": t_req}
        return plan

    def _history(self, order: dict, plan: dict, placed: datetime) -> None:
        def h(axis, frm, to, when, actor="SYSTEM"):
            self.add(
                "order_status_history",
                business_id=self.bid,
                order_id=order["id"],
                axis=axis,
                from_status=frm,
                to_status=to,
                actor_type=actor,
                actor_id=None,
                note=None,
                created_at=when,
            )

        h("ORDER", None, "PENDING_PAYMENT", placed, "CUSTOMER")
        h("PAYMENT", None, "UNPAID", placed)
        if plan.get("t_fail"):
            h("PAYMENT", "UNPAID", "FAILED", plan["t_fail"])
            h("ORDER", "PENDING_PAYMENT", "CANCELLED", plan["t_fail"])
            return
        if plan["order"] == "PENDING_PAYMENT":
            h("PAYMENT", "UNPAID", "PENDING", placed)
            return
        if order["payment_method"] == "ONLINE":
            h("PAYMENT", "UNPAID", "PENDING", placed)
            h("PAYMENT", "PENDING", "PAID", plan["t_paid"])
        h("ORDER", "PENDING_PAYMENT", "CONFIRMED", plan["t_paid"])
        prev = "CONFIRMED"
        for key, status, axis_to in (
            ("t_pack", "PACKED", "PACKED"),
            ("t_ship", "SHIPPED", "SHIPPED"),
            ("t_deliver", "DELIVERED", "DELIVERED"),
        ):
            if plan.get(key):
                h("ORDER", prev, status, plan[key], "OWNER")
                h(
                    "FULFILMENT",
                    "UNFULFILLED" if status == "PACKED" else prev,
                    axis_to,
                    plan[key],
                    "OWNER",
                )
                prev = status
        if order["payment_method"] == "COD" and plan.get("t_deliver"):
            h("PAYMENT", "UNPAID", "PAID", plan["t_deliver"])
        if plan.get("t_cancel"):
            h("ORDER", "CONFIRMED", "CANCELLED", plan["t_cancel"], "CUSTOMER")

    def _payment_and_stock(self, order, plan, reservations, method, day) -> None:
        r = self.r
        oid = order["id"]
        pay_amount = order["grand_total"]

        def day_of(moment):
            return (self.local_date(moment) - self.today).days

        if plan.get("t_fail"):
            self._payment(order, "FAILED", plan["t_fail"], method="upi", failure="BANK_DECLINED")
            self._webhook("payment.failed", order, plan["t_fail"])
            for res in reservations:
                self._schedule(
                    day_of(plan["t_fail"]),
                    (plan["t_fail"], self.seq("ev"), "release", {"res": res, "status": "RELEASED"}),
                )
            return
        if plan["order"] == "PENDING_PAYMENT":
            self._payment(order, "PENDING", order["placed_at"], method="upi")
            return
        t_paid = plan["t_paid"]
        if method == "ONLINE":
            self._payment(
                order,
                "CAPTURED",
                t_paid,
                method=r.choice(["upi", "upi", "card", "netbanking", "wallet"]),
            )
            self._webhook("payment.captured", order, t_paid)
            commit_at = t_paid
        else:
            commit_at = plan.get("t_ship")
        if commit_at:
            for res in reservations:
                self._schedule(
                    day_of(commit_at), (commit_at, self.seq("ev"), "commit", {"res": res})
                )
        if plan.get("t_cancel"):
            for res in reservations:
                if method == "ONLINE":
                    self._schedule(
                        day_of(plan["t_cancel"]),
                        (
                            plan["t_cancel"] + timedelta(seconds=1),
                            self.seq("ev"),
                            "restock_cancel",
                            {"res": res},
                        ),
                    )
                else:
                    self._schedule(
                        day_of(plan["t_cancel"]),
                        (
                            plan["t_cancel"],
                            self.seq("ev"),
                            "release",
                            {"res": res, "status": "RELEASED"},
                        ),
                    )
            if plan.get("t_refund"):
                pay = next(p for p in self.rows["payments"] if p["order_id"] == oid)
                self._refund(
                    order, pay, pay_amount, plan["t_refund"], "Order cancelled", f"cancel:{oid}"
                )
                pay["status"] = "REFUNDED"
        if method == "COD" and plan.get("t_deliver"):
            self._payment(order, "CAPTURED", plan["t_deliver"], method="cash", provider="cod")
        if plan.get("t_ship"):
            self.add(
                "shipments",
                id=self.uid(),
                business_id=self.bid,
                order_id=oid,
                carrier=r.choice(["Delhivery", "Blue Dart", "India Post", "Ecom Express"]),
                tracking_number=f"{r.randint(10**9, 10**10 - 1)}",
                status="DELIVERED" if plan.get("t_deliver") else "IN_TRANSIT",
                shipped_at=plan["t_ship"],
                delivered_at=plan.get("t_deliver"),
            )

    def _payment(self, order, status, when, *, method, failure=None, provider="fake") -> dict:
        n = self.seq("pay")
        return self.add(
            "payments",
            id=self.uid(),
            business_id=self.bid,
            order_id=order["id"],
            provider=provider,
            provider_order_id=f"{provider}_order_{n:06d}_{self.bid[:4]}",
            provider_payment_id=None
            if status == "PENDING"
            else f"{provider}_pay_{n:06d}_{self.bid[:4]}",
            method=method,
            status=status,
            amount=order["grand_total"],
            currency="INR",
            failure_code=failure,
            created_at=order["placed_at"] if provider == "fake" else when,
            updated_at=when,
        )

    def _webhook(self, kind: str, order: dict, when: datetime) -> None:
        n = self.seq("evt")
        self.add(
            "payment_webhook_events",
            provider="fake",
            business_id=self.bid,
            event_id=f"evt_{self.bid[:6]}_{n:07d}",
            event_type=kind,
            signature_valid=True,
            payload={"order_number": order["order_number"], "amount": str(order["grand_total"])},
            status="PROCESSED",
            processed_at=when + timedelta(seconds=2),
            error=None,
            received_at=when,
        )

    def _refund(self, order, payment, amount, when, reason, key) -> None:
        n = self.seq("rfnd")
        self.add(
            "refunds",
            id=self.uid(),
            business_id=self.bid,
            payment_id=payment["id"],
            order_id=order["id"],
            amount=amount,
            reason=reason,
            status="SUCCEEDED",
            idempotency_key=f"refund:{key}",
            provider_refund_id=f"fake_rfnd_{n:06d}",
            created_by=self.owner_ids["manager"],
            created_at=when,
        )
        self.add(
            "payment_webhook_events",
            provider="fake",
            business_id=self.bid,
            event_id=f"evt_{self.bid[:6]}_r{n:06d}",
            event_type="refund.processed",
            signature_valid=True,
            payload={"order_number": order["order_number"], "amount": str(amount)},
            status="PROCESSED",
            processed_at=when + timedelta(seconds=3),
            error=None,
            received_at=when + timedelta(seconds=1),
        )

    def _returns(self, order, plan, day) -> None:
        r = self.r
        t_req = plan["returns"]["t_req"]
        lim = self.now - timedelta(minutes=10)
        items = order["_items"]
        subset = (
            items
            if (len(items) == 1 or r.random() < 0.6)
            else r.sample(items, r.randint(1, len(items) - 1))
        )
        rid = self.uid()
        rejected = r.random() < 0.08
        t_decide = t_req + timedelta(hours=r.uniform(2, 24))
        t_recv = t_req + timedelta(days=r.uniform(2, 5))
        t_refund = t_recv + timedelta(days=r.uniform(0.2, 1.0))
        status = "REQUESTED"
        if rejected and t_decide <= lim:
            status = "REJECTED"
        elif not rejected:
            if t_decide <= lim:
                status = "APPROVED"
            if t_recv <= lim:
                status = "RECEIVED"
            if t_refund <= lim:
                status = "REFUNDED"
        order["order_status"] = (
            "DELIVERED"
            if status == "REJECTED"
            else ("RETURNED" if status in ("RECEIVED", "REFUNDED") else "RETURN_REQUESTED")
        )
        order["updated_at"] = {
            "REQUESTED": t_req,
            "APPROVED": t_decide,
            "REJECTED": t_decide,
            "RECEIVED": t_recv,
            "REFUNDED": t_refund,
        }[status]
        self.add(
            "return_requests",
            id=rid,
            business_id=self.bid,
            order_id=order["id"],
            status=status,
            reason=r.choice(
                ["Size/fit issue", "Defective item", "Not as described", "Changed my mind"]
            ),
            requested_by=order["customer_id"],
            decided_by=None if status == "REQUESTED" else self.owner_ids["manager"],
            created_at=t_req,
            updated_at=order["updated_at"],
        )
        self.add(
            "order_status_history",
            business_id=self.bid,
            order_id=order["id"],
            axis="ORDER",
            from_status="DELIVERED",
            to_status="RETURN_REQUESTED",
            actor_type="CUSTOMER",
            actor_id=None,
            note=None,
            created_at=t_req,
        )
        rec_items = []
        for oi, v, qty, line in subset:
            rid_item = self.uid()
            condition = "RESELLABLE" if r.random() < 0.85 else "DAMAGED"
            self.add(
                "return_request_items",
                id=rid_item,
                return_id=rid,
                business_id=self.bid,
                order_item_id=oi["id"],
                quantity=qty,
                condition=condition,
            )
            rec_items.append(
                {
                    "res": oi["_res"],
                    "qty": qty,
                    "condition": condition,
                    "row_id": rid_item,
                    "line": line,
                    "oi": oi,
                }
            )
        if status in ("RECEIVED", "REFUNDED"):
            self.add(
                "order_status_history",
                business_id=self.bid,
                order_id=order["id"],
                axis="ORDER",
                from_status="RETURN_REQUESTED",
                to_status="RETURNED",
                actor_type="OWNER",
                actor_id=None,
                note=None,
                created_at=t_recv,
            )
            self._schedule(
                (self.local_date(t_recv) - self.today).days,
                (t_recv, self.seq("ev"), "return_receive", {"items": rec_items, "return_id": rid}),
            )
        if status == "REFUNDED":
            full = len(subset) == len(items)
            pay = next((p for p in self.rows["payments"] if p["order_id"] == order["id"]), None)
            if pay is None:  # COD order: payment row is created at delivery; refund against it
                pay = self._payment(
                    order, "CAPTURED", plan["t_deliver"], method="cash", provider="cod"
                )
            # a refund can never exceed what was captured (round-off can push the line sum a few paise above it)
            refund_amount = min(sum((i["line"].line_total for i in rec_items), ZERO), pay["amount"])
            self._refund(order, pay, refund_amount, t_refund, "Return received", f"return:{rid}")
            pay["status"] = "REFUNDED" if full else "PARTIALLY_REFUNDED"
            order["payment_status"] = pay["status"]
            self.pending_credit_notes.append(
                {
                    "order": order,
                    "items": rec_items,
                    "when": t_refund,
                    "reason": "Goods returned",
                    "full": full,
                }
            )

    def _place_pending_orders(self) -> None:
        n = 4 if self.scale >= 0.5 else 2
        for _ in range(n):
            when = self.now - timedelta(minutes=self.r.randint(1, 12))
            self._place_order(when, 0, None, forced_pending=True)

    # ------------------------------------------------------------------ end-of-history stock shaping
    def _cycle_counts_and_write_offs(self) -> None:
        when = self.now - timedelta(hours=3)
        lines = []
        for v in self.variants:
            if v["status"] != "PUBLISHED":
                continue
            total = self._avail(v)
            if v["persona"] == "STOCKOUT" and total > 0:
                for w in self.warehouses:
                    b = self._balance(v["id"], w["id"])
                    free = b["on_hand"] - b["reserved"]
                    if free > 0:
                        lines.append((v, w["id"], -free))
            elif v["persona"] == "LOW" and total > v["thr"]:
                target = D(self.r.randint(1, int(v["thr"])))
                drop = total - target
                for w in self.warehouses:
                    b = self._balance(v["id"], w["id"])
                    free = b["on_hand"] - b["reserved"]
                    take = min(free, drop)
                    if take > 0:
                        lines.append((v, w["id"], -take))
                        drop -= take
        if lines:
            self._post_adjustment(when, "CYCLE_COUNT_WRITE_OFF", lines, kind="ADJUSTMENT")
        # one reviewed-but-not-posted count (DRAFT) so the workflow has an open item
        v = next(
            (x for x in self.variants if x["status"] == "PUBLISHED" and self._avail(x) > 5), None
        )
        if v:
            wh = self._wh_with_stock(v, D(1))
            aid = self.uid()
            on_hand = self._balance(v["id"], wh)["on_hand"]
            self.add(
                "stock_adjustments",
                id=aid,
                business_id=self.bid,
                reason_code="CYCLE_COUNT",
                note="Count in progress (not posted)",
                status="DRAFT",
                created_by=self.owner_ids["staff"],
                approved_by=None,
                posted_at=None,
                created_at=self.now - timedelta(hours=2),
            )
            self.add(
                "stock_adjustment_lines",
                id=self.uid(),
                adjustment_id=aid,
                business_id=self.bid,
                variant_id=v["id"],
                warehouse_id=wh,
                counted_qty=on_hand - 2,
                delta_qty=D(-2),
            )
        # purchase return with a debit note
        receipts = [rc for rcs in self.receipts_by_po.values() for rc in rcs]
        for idx, rc in enumerate(receipts[:2]):
            items = [i for i in self.rows["goods_receipt_items"] if i["receipt_id"] == rc["id"]][:1]
            for it in items:
                avail = self._balance(it["variant_id"], rc["warehouse_id"])
                qty = D(min(2, int(avail["on_hand"] - avail["reserved"])))
                if qty <= 0:
                    continue
                prid = self.uid()
                when_r = self.now - timedelta(hours=2 + idx)
                self.add(
                    "purchase_returns",
                    id=prid,
                    business_id=self.bid,
                    supplier_id=rc["supplier_id"],
                    warehouse_id=rc["warehouse_id"],
                    number=f"PR-{idx + 1:05d}",
                    status="POSTED",
                    reason="Defective on receipt",
                    created_at=when_r,
                )
                self.add(
                    "purchase_return_items",
                    id=self.uid(),
                    return_id=prid,
                    business_id=self.bid,
                    variant_id=it["variant_id"],
                    quantity=qty,
                    unit_cost=it["unit_cost"],
                )
                self._ledger(
                    when_r,
                    it["variant_id"],
                    rc["warehouse_id"],
                    "PURCHASE_RETURN",
                    -qty,
                    reason="DEFECTIVE",
                    source="PURCHASE_RETURN",
                    source_id=prid,
                    key=f"pret:{prid}",
                    actor="OWNER",
                    actor_id=self.owner_ids["manager"],
                )
                self.add(
                    "purchase_debit_notes",
                    id=self.uid(),
                    business_id=self.bid,
                    supplier_id=rc["supplier_id"],
                    purchase_return_id=prid,
                    purchase_invoice_id=None,
                    number=f"DN-{idx + 1:05d}",
                    amount=round2(qty * it["unit_cost"]),
                    reason="Defective on receipt",
                    created_at=when_r,
                )

    def _finalize_balances(self) -> None:
        for (v_id, wh), b in self.bal.items():
            if not b["touched"]:
                continue
            self.add(
                "inventory_balances",
                business_id=self.bid,
                variant_id=v_id,
                warehouse_id=wh,
                on_hand=b["on_hand"],
                reserved=b["reserved"],
                avg_cost=b["avg"],
                version=0,
                updated_at=self.now,
            )
        for res in self.rows["stock_reservations"]:
            res["updated_at"] = res.get("updated_at") or self.now

    # ------------------------------------------------------------------ purchasing documents
    def _purchasing_documents(self) -> None:
        r = self.r
        sup_by_id = {s["id"]: s for s in self.suppliers}
        variants = {v["id"]: v for v in self.variants}
        for po in list(self.rows["purchase_orders"]):
            receipts = self.receipts_by_po.get(po["id"])
            if not receipts or r.random() < 0.15:
                continue
            sup = sup_by_id[po["supplier_id"]]
            items = [
                i for i in self.rows["purchase_order_items"] if i["purchase_order_id"] == po["id"]
            ]
            taxable = sum((round2(i["received_quantity"] * i["unit_cost"]) for i in items), ZERO)
            tax = ZERO
            for i in items:
                rate = rates_on(variants[i["variant_id"]]["p"].tax, self.today)
                if rate:
                    tax += round2(
                        round2(i["received_quantity"] * i["unit_cost"])
                        * (rate[3] if rate[3] else rate[1] + rate[2])
                        / 100
                    )
            inv_date = self.local_date(receipts[-1]["received_at"])
            doc_id = self.uid()
            sha = hashlib.sha256(f"seed-doc:{doc_id}".encode()).hexdigest()
            self.add(
                "documents",
                id=doc_id,
                business_id=self.bid,
                kind="PURCHASE_INVOICE",
                storage_path=f"seed://purchase-invoices/{doc_id}.pdf",
                original_name=f"{po['number']}-invoice.pdf",
                mime="application/pdf",
                byte_size=r.randint(40_000, 180_000),
                sha256=sha,
                uploaded_by=self.owner_ids["manager"],
                created_at=receipts[-1]["received_at"] + timedelta(hours=3),
            )
            roll = r.random()
            status = (
                "APPROVED"
                if roll < 0.82
                else "NEEDS_REVIEW"
                if roll < 0.92
                else "DRAFT"
                if roll < 0.97
                else "REJECTED"
            )
            grand = taxable + tax
            extraction = confidence = None
            history = None
            if status in ("NEEDS_REVIEW", "DRAFT"):
                misread = grand + D(r.choice([-100, 90, 450, -250]))
                extraction = {
                    "supplier": sup["name"],
                    "invoice_number": f"INV-{r.randint(1000, 9999)}",
                    "invoice_date": inv_date.isoformat(),
                    "grand_total": str(misread),
                    "source": "ai_extraction_sample",
                }
                confidence = {
                    "supplier": 0.97,
                    "invoice_number": 0.93,
                    "invoice_date": 0.88,
                    "grand_total": 0.46,
                }
            if status == "APPROVED":
                history = (
                    [
                        {
                            "at": (receipts[-1]["received_at"] + timedelta(hours=5)).isoformat(),
                            "field": "invoice_number",
                            "note": "Entered manually",
                        }
                    ]
                    if r.random() < 0.3
                    else []
                )
            pi_id = self.uid()
            self.add(
                "purchase_invoices",
                id=pi_id,
                business_id=self.bid,
                supplier_id=sup["id"],
                document_id=doc_id,
                status=status,
                extraction=extraction,
                field_confidence=confidence,
                supplier_invoice_number=f"{sup['name'].split()[0][:3].upper()}/{inv_date:%y%m}/{r.randint(100, 999)}",
                invoice_date=inv_date,
                due_date=inv_date + timedelta(days=sup["terms"]),
                taxable_total=taxable,
                tax_total=tax,
                grand_total=grand,
                linked_po_id=po["id"],
                approved_by=self.owner_ids["owner"] if status == "APPROVED" else None,
                approved_at=receipts[-1]["received_at"] + timedelta(days=1)
                if status == "APPROVED"
                else None,
                correction_history=history,
                created_at=receipts[-1]["received_at"] + timedelta(hours=3),
                updated_at=receipts[-1]["received_at"] + timedelta(days=1),
            )
            if status == "APPROVED":
                due = inv_date + timedelta(days=sup["terms"])
                paid_roll = r.random()
                if due < self.today - timedelta(days=3):
                    pstat = "PAID" if paid_roll < 0.8 else "PARTIAL" if paid_roll < 0.9 else "DUE"
                else:
                    pstat = "PAID" if paid_roll < 0.25 else "DUE"
                paid = (
                    grand
                    if pstat == "PAID"
                    else round2(grand * D("0.5"))
                    if pstat == "PARTIAL"
                    else ZERO
                )
                self.add(
                    "supplier_payments",
                    id=self.uid(),
                    business_id=self.bid,
                    supplier_id=sup["id"],
                    purchase_invoice_id=pi_id,
                    status=pstat,
                    amount=grand,
                    amount_paid=paid,
                    due_date=due,
                    paid_at=(
                        datetime.combine(due, time(11)) - timedelta(days=r.randint(0, 4)) - IST
                    )
                    if paid
                    else None,
                    reference=f"UTR{r.randint(10**9, 10**10 - 1)}" if paid else None,
                    created_at=receipts[-1]["received_at"] + timedelta(days=1),
                )

    # ------------------------------------------------------------------ invoices
    def _invoices_and_credit_notes(self) -> None:
        spec = self.spec
        seller = {
            "name": spec.name,
            "legal_name": spec.legal_name,
            "gstin": spec.gstin,
            "address": spec.address,
            "state_code": spec.state_code,
        }
        doc_type = "TAX_INVOICE" if spec.gst_registered else "BILL_OF_SUPPLY"
        seqs: dict[tuple[str, str], int] = defaultdict(int)
        invoices_by_order: dict[str, dict] = {}
        for order in sorted(self.pending_invoices, key=lambda o: o["_plan"]["t_ship"]):
            plan, quote, customer = order["_plan"], order["_quote"], order["_customer"]
            issued = plan["t_ship"]
            fy = fy_of(self.local_date(issued))
            series = spec.invoice_prefix
            seqs[(fy, series)] += 1
            number = seqs[(fy, series)]
            display = f"{series}/{fy[2:4]}-{fy[-2:]}/{number:05d}"
            buyer = {
                "name": customer["name"],
                "address": f"{customer['street']}, {customer['city']}",
                "state_code": customer["state"],
                "gstin": customer["gstin"],
            }
            lines = []
            for pos, (oi, v, qty, line) in enumerate(order["_items"], start=1):
                lines.append(
                    {
                        "position": pos,
                        "description": oi["product_name"]
                        + (f" ({', '.join(v['opts'].values())})" if v["opts"] else ""),
                        "hsn_sac": oi["hsn_sac"],
                        "quantity": qty,
                        "unit": oi["stock_unit"],
                        "unit_price": oi["unit_price"],
                        "discount": line.discount,
                        "taxable": line.taxable,
                        "cgst": line.cgst,
                        "sgst": line.sgst,
                        "igst": line.igst,
                        "cess": line.cess,
                        "rate": line.rate_pct,
                        "total": line.line_total,
                    }
                )
            if quote.shipping:
                s = quote.shipping
                lines.append(
                    {
                        "position": len(lines) + 1,
                        "description": "Shipping and handling",
                        "hsn_sac": SHIPPING_SAC,
                        "quantity": D(1),
                        "unit": "PCS",
                        "unit_price": s.gross,
                        "discount": ZERO,
                        "taxable": s.taxable,
                        "cgst": s.cgst,
                        "sgst": s.sgst,
                        "igst": s.igst,
                        "cess": s.cess,
                        "rate": s.rate_pct,
                        "total": s.line_total,
                    }
                )
            totals = {
                k: sum((line[k] for line in lines), ZERO)
                for k in ("taxable", "cgst", "sgst", "igst", "cess")
            }
            inv_id = self.uid()
            digest = canonical_hash(
                {
                    "b": self.bid,
                    "n": display,
                    "seller": seller,
                    "buyer": buyer,
                    "lines": lines,
                    "grand": quote.grand_total,
                }
            )
            invoice = self.add(
                "invoices",
                id=inv_id,
                business_id=self.bid,
                order_id=order["id"],
                customer_id=order["customer_id"],
                doc_type=doc_type,
                series=series,
                fy=fy,
                number=number,
                display_number=display,
                status="ISSUED",
                issued_at=issued,
                seller=seller,
                buyer=buyer,
                place_of_supply_state=order["place_of_supply_state"],
                is_inter_state=customer["state"] != spec.state_code,
                taxable_total=totals["taxable"],
                cgst_total=totals["cgst"],
                sgst_total=totals["sgst"],
                igst_total=totals["igst"],
                cess_total=totals["cess"],
                round_off=quote.round_off,
                grand_total=quote.grand_total,
                prices_include_tax=spec.prices_include_tax,
                tax_rate_version_ids=quote.version_ids,
                content_hash=digest,
                pdf_storage_path=None,
                pdf_sha256=None,
                created_at=issued,
            )
            invoices_by_order[order["id"]] = invoice
            invoice["_lines"] = lines
            for ln in lines:
                self.add(
                    "invoice_lines",
                    id=self.uid(),
                    business_id=self.bid,
                    invoice_id=inv_id,
                    position=ln["position"],
                    description=ln["description"],
                    hsn_sac=ln["hsn_sac"],
                    quantity=ln["quantity"],
                    unit=ln["unit"],
                    unit_price=ln["unit_price"],
                    discount=ln["discount"],
                    taxable_value=ln["taxable"],
                    cgst_rate=ln["rate"] / 2 if (ln["cgst"] and not ln["igst"]) else ZERO,
                    cgst=ln["cgst"],
                    sgst_rate=ln["rate"] / 2 if (ln["sgst"] and not ln["igst"]) else ZERO,
                    sgst=ln["sgst"],
                    igst_rate=ln["rate"] if ln["igst"] else ZERO,
                    igst=ln["igst"],
                    cess_rate=ZERO,
                    cess=ln["cess"],
                    line_total=ln["total"],
                )
        cn_seq: dict[str, int] = defaultdict(int)
        for note in sorted(self.pending_credit_notes, key=lambda n: n["when"]):
            invoice = invoices_by_order.get(note["order"]["id"])
            if invoice is None:
                continue
            fy = fy_of(self.local_date(note["when"]))
            cn_seq[fy] += 1
            number = cn_seq[fy]
            display = f"CN/{fy[2:4]}-{fy[-2:]}/{number:05d}"
            taxable = cgst = sgst = igst = cess = total = ZERO
            note_lines = []
            for item in note["items"]:
                line = item["line"]
                note_lines.append((item["oi"], line))
                taxable += line.taxable
                cgst += line.cgst
                sgst += line.sgst
                igst += line.igst
                cess += line.cess
                total += line.line_total
            grand = round_rupee(total)
            cn_id = self.uid()
            self.add(
                "credit_notes",
                id=cn_id,
                business_id=self.bid,
                invoice_id=invoice["id"],
                series="CN",
                fy=fy,
                number=number,
                display_number=display,
                reason=note["reason"],
                issued_at=note["when"],
                taxable_total=taxable,
                cgst_total=cgst,
                sgst_total=sgst,
                igst_total=igst,
                cess_total=cess,
                round_off=grand - total,
                grand_total=grand,
                content_hash=canonical_hash(
                    {"cn": display, "invoice": invoice["display_number"], "grand": grand}
                ),
                created_at=note["when"],
            )
            for oi, line in note_lines:
                self.add(
                    "credit_note_lines",
                    id=self.uid(),
                    business_id=self.bid,
                    credit_note_id=cn_id,
                    description=oi["product_name"],
                    hsn_sac=oi["hsn_sac"],
                    quantity=oi["quantity"],
                    taxable_value=line.taxable,
                    cgst=line.cgst,
                    sgst=line.sgst,
                    igst=line.igst,
                    cess=line.cess,
                    line_total=line.line_total,
                )
        for (fy, series), last in seqs.items():
            self.add(
                "invoice_sequences", business_id=self.bid, fy=fy, series=series, last_number=last
            )
        for fy, last in cn_seq.items():
            self.add(
                "invoice_sequences", business_id=self.bid, fy=fy, series="CN", last_number=last
            )

    # ------------------------------------------------------------------ stub tenants (no activity)
    def _storefront_stub(self) -> None:
        pass
