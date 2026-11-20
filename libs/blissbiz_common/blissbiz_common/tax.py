"""Pure GST engine (docs/04 section 7). Exact Decimal arithmetic; rates are injected, so the billing
service can read them from its ``tax_rate_versions`` table and tests can supply fixed rows.

Inclusive prices: taxable = round2(gross / (1 + rate)), tax = gross - taxable
(so the line total is the shelf price).
Exclusive prices: tax = round2(taxable * rate) is added on top.
CGST = round2(tax / 2), SGST = the remainder.
Unregistered sellers charge no tax. Shipping is a taxable service line. Totals round to the rupee.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
SHIPPING_SAC = "996812"
SHIPPING_TAX_CLASS = "GST_18"


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round2(value) -> Decimal:
    return D(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def round_rupee(value) -> Decimal:
    return D(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Rate:
    version_id: str
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    cess: Decimal


RateLookup = Callable[[str, date], Rate | None]


@dataclass
class QuotedLine:
    gross: Decimal
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

    def as_json(self) -> dict:
        return {
            "gross": str(self.gross),
            "discount": str(self.discount),
            "taxable": str(self.taxable),
            "cgst": str(self.cgst),
            "sgst": str(self.sgst),
            "igst": str(self.igst),
            "cess": str(self.cess),
            "tax": str(self.tax),
            "line_total": str(self.line_total),
            "rate_pct": str(self.rate_pct),
            "tax_rate_version_id": self.version_id,
        }


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

    def as_json(self) -> dict:
        return {
            "lines": [line.as_json() for line in self.lines],
            "shipping": self.shipping.as_json() if self.shipping else None,
            "subtotal": str(self.subtotal),
            "discount_total": str(self.discount_total),
            "tax_total": str(self.tax_total),
            "shipping_total": str(self.shipping_total),
            "round_off": str(self.round_off),
            "grand_total": str(self.grand_total),
            "tax_rate_version_ids": self.version_ids,
        }


def _tax_line(net, gross, discount, tax_class, *, rates, registered, inter_state, include_tax, on):
    rate = rates(tax_class, on) if registered else None
    if rate is None or (rate.cgst + rate.sgst + rate.igst + rate.cess) == 0:
        return QuotedLine(gross, discount, net, line_total=net)
    pct = rate.igst + rate.cess if inter_state else rate.cgst + rate.sgst + rate.cess
    if include_tax:
        taxable = round2(net / (1 + pct / 100))
        tax = net - taxable
    else:
        taxable = net
        tax = round2(taxable * pct / 100)
    cess = round2(taxable * rate.cess / 100) if rate.cess else ZERO
    core = tax - cess
    if inter_state:
        cgst = sgst = ZERO
        igst = core
    else:
        cgst = round2(core / 2)
        sgst = core - cgst
        igst = ZERO
    return QuotedLine(
        gross, discount, taxable, cgst, sgst, igst, cess, taxable + tax, pct, rate.version_id
    )


def quote(
    items: list[tuple[Decimal, Decimal, str]],
    *,
    rates: RateLookup,
    seller_registered: bool,
    inter_state: bool,
    include_tax: bool,
    discount_pct: Decimal = ZERO,
    shipping_gross: Decimal = ZERO,
    on: date,
) -> Quote:
    """items = [(quantity, unit_price, tax_class)]. discount_pct applies to every goods line."""
    common = dict(
        rates=rates,
        registered=seller_registered,
        inter_state=inter_state,
        include_tax=include_tax,
        on=on,
    )
    lines = []
    for qty, unit_price, tax_class in items:
        gross = round2(D(qty) * D(unit_price))
        discount = round2(gross * D(discount_pct) / 100)
        lines.append(_tax_line(gross - discount, gross, discount, tax_class, **common))
    shipping = None
    if D(shipping_gross) > 0:
        shipping = _tax_line(
            D(shipping_gross), D(shipping_gross), ZERO, SHIPPING_TAX_CLASS, **common
        )
    everything = lines + ([shipping] if shipping else [])
    result = Quote(lines, shipping)
    result.subtotal = sum((line.gross for line in lines), ZERO)
    result.discount_total = sum((line.discount for line in lines), ZERO)
    result.tax_total = sum((line.tax for line in everything), ZERO)
    result.shipping_total = shipping.gross if shipping else ZERO
    raw = sum((line.line_total for line in everything), ZERO)
    result.grand_total = round_rupee(raw)
    result.round_off = result.grand_total - raw
    result.version_ids = sorted({line.version_id for line in everything if line.version_id})
    return result
