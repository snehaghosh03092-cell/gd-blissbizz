"""The shared tax engine must give exactly the numbers of the seed's verified calculator."""

import random
from datetime import date
from decimal import Decimal

import pytest

from blissbiz_common.cache import TTLCache
from blissbiz_common.tax import Rate, quote

ON = date(2026, 11, 18)
RATES = {
    "GST_0": Rate("v0", Decimal(0), Decimal(0), Decimal(0), Decimal(0)),
    "GST_5": Rate("v5", Decimal("2.5"), Decimal("2.5"), Decimal(5), Decimal(0)),
    "GST_18": Rate("v18", Decimal(9), Decimal(9), Decimal(18), Decimal(0)),
    "GST_40": Rate("v40", Decimal(20), Decimal(20), Decimal(40), Decimal(0)),
}


def rates(tax_class, on):
    return RATES.get(tax_class)


def test_inclusive_intra_state_with_discount_and_shipping():
    q = quote(
        [(Decimal(2), Decimal("499"), "GST_5"), (Decimal(1), Decimal("1799"), "GST_5")],
        rates=rates,
        seller_registered=True,
        inter_state=False,
        include_tax=True,
        discount_pct=Decimal(10),
        shipping_gross=Decimal(49),
        on=ON,
    )
    first = q.lines[0]
    assert (first.gross, first.discount, first.taxable, first.cgst, first.sgst) == (
        Decimal("998.00"),
        Decimal("99.80"),
        Decimal("855.43"),
        Decimal("21.39"),
        Decimal("21.38"),
    )
    assert first.line_total == Decimal("898.20")  # the shelf price after discount
    assert q.shipping.taxable == Decimal("41.53") and q.shipping.line_total == Decimal("49")
    assert (q.grand_total, q.round_off) == (Decimal("2566"), Decimal("-0.30"))


def test_exclusive_inter_state_adds_igst_on_top():
    q = quote(
        [(Decimal(1), Decimal("3299"), "GST_18")],
        rates=rates,
        seller_registered=True,
        inter_state=True,
        include_tax=False,
        on=ON,
    )
    line = q.lines[0]
    assert (line.taxable, line.igst, line.cgst, line.line_total) == (
        Decimal("3299.00"),
        Decimal("593.82"),
        0,
        Decimal("3892.82"),
    )
    assert (q.grand_total, q.round_off) == (Decimal("3893"), Decimal("0.18"))


def test_unregistered_sellers_charge_no_tax():
    q = quote(
        [(Decimal(1), Decimal("1199"), "GST_18")],
        rates=rates,
        seller_registered=False,
        inter_state=False,
        include_tax=True,
        on=ON,
    )
    assert q.tax_total == 0 and q.grand_total == Decimal("1199")


def test_matches_the_seed_calculator_on_random_orders():
    """2,000 random orders give exactly the results of app/seed/taxes.py (the seed's calculator)."""
    seed_taxes = pytest.importorskip("app.seed.taxes")
    rng = random.Random(7)
    for _ in range(2000):
        items = [
            (
                Decimal(rng.choice(["0.5", "1", "2", "3"])),
                Decimal(str(rng.randint(10, 9999))),
                rng.choice(list(RATES)),
            )
            for _ in range(rng.randint(1, 4))
        ]
        kwargs = dict(
            seller_registered=rng.random() < 0.7,
            inter_state=rng.random() < 0.4,
            include_tax=rng.random() < 0.7,
            discount_pct=Decimal(rng.choice([0, 5, 10, 15])),
            shipping_gross=Decimal(rng.choice([0, 29, 49, 99])),
            on=ON,
        )
        ours = quote(items, rates=rates, **kwargs)
        theirs = seed_taxes.quote_order(items, **kwargs)
        assert (ours.grand_total, ours.tax_total, ours.round_off) == (
            theirs.grand_total,
            theirs.tax_total,
            theirs.round_off,
        )
        assert [line.line_total for line in ours.lines] == [
            line.line_total for line in theirs.lines
        ]


def test_ttl_cache_expires():
    now = [0.0]
    cache = TTLCache(10, clock=lambda: now[0])
    cache.set("k", 1)
    assert cache.get("k") == 1
    now[0] = 11
    assert cache.get("k") is None
