"""Seed-side tax calculator (mirrors docs/04 section 7). The billing service will own the real engine; its
golden tests should reproduce these numbers. Handles tax-inclusive and tax-exclusive pricing, intra/inter-state
splits, per-line discount, shipping as a taxable line (SAC 996812), and round-off to the nearest rupee.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.core.money import ZERO, D, round2, round_rupee
from app.seed.reference import RATE_ROWS, rate_version_id

SHIPPING_SAC = "996812"
SHIPPING_TAX_CLASS = "GST_18"


def rates_on(tax_class: str, on: date):
    """(version_id, cgst%, sgst%, igst%, cess%) in force on ``on`` or None (exempt/none)."""
    for c, cg, sg, ig, cess, frm, to in RATE_ROWS:
        if c == tax_class and frm <= on and (to is None or on <= to):
            return rate_version_id(c, frm), D(cg), D(sg), D(ig), D(cess)
    return None


@dataclass
class QuotedLine:
    gross: Decimal  # qty * unit_price before discount
    discount: Decimal
    taxable: Decimal
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO
    cess: Decimal = ZERO
    line_total: Decimal = ZERO
    rate_pct: Decimal = ZERO
    version_id: str | None = None

    @property
    def tax(self) -> Decimal:
        return self.cgst + self.sgst + self.igst + self.cess


@dataclass
class Quote:
    lines: list[QuotedLine]
    shipping: QuotedLine | None
    subtotal: Decimal = ZERO
    discount_total: Decimal = ZERO
    tax_total: Decimal = ZERO
    shipping_total: Decimal = ZERO
    round_off: Decimal = ZERO
    grand_total: Decimal = ZERO
    version_ids: list[str] = field(default_factory=list)


def _tax_line(
    net: Decimal,
    gross: Decimal,
    discount: Decimal,
    tax_class: str,
    *,
    registered: bool,
    inter_state: bool,
    include_tax: bool,
    on: date,
) -> QuotedLine:
    """``net`` = amount after discount as entered (tax-inclusive or tax-exclusive per ``include_tax``)."""
    rate = rates_on(tax_class, on) if registered else None
    if rate is None or (rate[1] + rate[2] + rate[3] + rate[4]) == 0:
        return QuotedLine(gross, discount, net, line_total=net)
    version, cg, sg, ig, cess = rate
    pct = ig + cess if inter_state else cg + sg + cess
    if include_tax:
        taxable = round2(net / (1 + pct / 100))
        tax = net - taxable
    else:
        taxable = net
        tax = round2(taxable * pct / 100)
    cess_amount = round2(taxable * cess / 100) if cess else ZERO
    core_tax = tax - cess_amount
    if inter_state:
        c = s = ZERO
        i = core_tax
    else:
        c = round2(core_tax / 2)
        s = core_tax - c
        i = ZERO
    return QuotedLine(gross, discount, taxable, c, s, i, cess_amount, taxable + tax, pct, version)


def quote_order(
    items: list[tuple[Decimal, Decimal, str]],
    *,
    seller_registered: bool,
    inter_state: bool,
    include_tax: bool,
    discount_pct: Decimal,
    shipping_gross: Decimal,
    on: date,
) -> Quote:
    """items = [(quantity, unit_price, tax_class)]; discount_pct is a percentage applied to every goods line."""
    lines: list[QuotedLine] = []
    for qty, unit_price, tax_class in items:
        gross = round2(qty * unit_price)
        discount = round2(gross * discount_pct / 100)
        lines.append(
            _tax_line(
                gross - discount,
                gross,
                discount,
                tax_class,
                registered=seller_registered,
                inter_state=inter_state,
                include_tax=include_tax,
                on=on,
            )
        )
    shipping = None
    if shipping_gross > 0:
        shipping = _tax_line(
            shipping_gross,
            shipping_gross,
            ZERO,
            SHIPPING_TAX_CLASS,
            registered=seller_registered,
            inter_state=inter_state,
            include_tax=include_tax,
            on=on,
        )
    everything = lines + ([shipping] if shipping else [])
    quote = Quote(lines, shipping)
    quote.subtotal = sum((line.gross for line in lines), ZERO)
    quote.discount_total = sum((line.discount for line in lines), ZERO)
    quote.tax_total = sum((line.tax for line in everything), ZERO)
    quote.shipping_total = shipping.gross if shipping else ZERO
    raw = sum((line.line_total for line in everything), ZERO)
    quote.grand_total = round_rupee(raw)
    quote.round_off = quote.grand_total - raw
    quote.version_ids = sorted({line.version_id for line in everything if line.version_id})
    return quote
