"""Turning a question into structured filters, deterministically (no LLM): words, colours,
sizes, price limits and time periods. Shared by the shopper and owner assistants."""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

WORD = re.compile(r"[a-z0-9]+")
STOP = {
    "a",
    "an",
    "the",
    "for",
    "with",
    "and",
    "or",
    "in",
    "of",
    "me",
    "show",
    "i",
    "want",
    "some",
    "any",
    "please",
    "find",
    "looking",
    "need",
    "do",
    "you",
    "have",
    "is",
    "are",
    "there",
    "my",
    "what",
    "which",
    "how",
    "many",
    "much",
    "can",
    "get",
    "buy",
    "to",
    "it",
    "this",
    "that",
    "color",
    "colour",
    "size",
    "price",
    "rs",
    "inr",
    "rupees",
    "rupee",
    "under",
    "below",
    "less",
    "than",
    "above",
    "over",
    "more",
    "between",
    "within",
    "upto",
    "up",
    "available",
    "stock",
    "left",
    "items",
    "item",
    "products",
    "product",
    "something",
    "anything",
    "on",
    "at",
    "by",
    "your",
    "we",
    "our",
    "us",
    "all",
    "list",
    "tell",
    "about",
    "store",
    "shop",
    "today",
    "yesterday",
    "week",
    "month",
    "year",
    "last",
    "this",
    "past",
    "days",
    "day",
    "now",
    "currently",
    "still",
    "also",
    "like",
    "would",
    "pairs",
    "pair",
    "ok",
    "okay",
    "maybe",
    "help",
    "suitable",
}
SIZES = {"xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl", "small", "medium", "large", "free"}
SIZE_ALIASES = {"small": "s", "medium": "m", "large": "l"}

# Shoppers often use a family name that is not literally present in a merchant's
# catalog.  For example, a catalog may call its category "T-Shirts" while the
# shopper asks for "tops".  Keep this deliberately small and retail-specific:
# it broadens a DB lookup, but never invents products or product attributes.
PRODUCT_TERM_GROUPS = (
    frozenset({"top", "tee", "tshirt", "shirt", "blouse", "kurta"}),
    frozenset({"bottom", "pant", "trouser", "jean", "short", "skirt"}),
    frozenset({"shoe", "sneaker", "footwear", "sandal", "slipper", "boot"}),
)


def stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("es") and word[:-2].endswith(("s", "x", "ch", "sh")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(*texts) -> set[str]:
    out: set[str] = set()
    for t in texts:
        if t:
            out |= {stem(w) for w in WORD.findall(str(t).lower())}
    return out


def product_term_matches(term: str, haystack: set[str]) -> bool:
    """Whether one shopper term is represented by DB-backed catalog words."""
    if term in haystack:
        return True
    if term == "ethnic":
        return bool(haystack & {"kurta", "anarkali", "saree", "sari", "lehenga"})
    return any(term in group and bool(group & haystack) for group in PRODUCT_TERM_GROUPS)


def money_value(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


PRICE_UNDER = re.compile(
    r"(?:under|below|less than|upto|up to|within|cheaper than|max(?:imum)?|<)\s*(?:rs\.?|₹|inr)?\s*([\d,]+)",
    re.I,
)
PRICE_OVER = re.compile(r"(?:above|over|more than|at least|min(?:imum)?|>)\s*(?:rs\.?|₹|inr)?\s*([\d,]+)", re.I)
PRICE_BETWEEN = re.compile(r"between\s*(?:rs\.?|₹)?\s*([\d,]+)\s*(?:and|to|-)\s*(?:rs\.?|₹)?\s*([\d,]+)", re.I)


def price_range(text: str) -> tuple[Decimal | None, Decimal | None]:
    m = PRICE_BETWEEN.search(text)
    if m:
        a, b = sorted((money_value(m.group(1)), money_value(m.group(2))))
        return a, b
    low = PRICE_OVER.search(text)
    high = PRICE_UNDER.search(text)
    return (
        money_value(low.group(1)) if low else None,
        money_value(high.group(1)) if high else None,
    )


@dataclass(frozen=True)
class Period:
    label: str
    start: date
    end: date  # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def period(text: str, today: date, default: str = "last 30 days") -> Period:
    t = text.lower()
    if "today" in t:
        return Period("today", today, today)
    if "yesterday" in t:
        return Period("yesterday", today - timedelta(days=1), today - timedelta(days=1))
    if re.search(r"this week", t):
        return Period("this week", today - timedelta(days=today.weekday()), today)
    if re.search(r"last week|past week|(?:last|past) 7 days|\bweek\b|weekly", t):
        return Period("the last 7 days", today - timedelta(days=6), today)
    if re.search(r"this month", t):
        return Period("this month", today.replace(day=1), today)
    if re.search(r"last month|(?:last|past) 30 days|\bmonth\b|monthly", t):
        return Period("the last 30 days", today - timedelta(days=29), today)
    if re.search(r"(?:last|past) 90 days|quarter", t):
        return Period("the last 90 days", today - timedelta(days=89), today)
    if re.search(r"this year|\byear\b", t):
        return Period("this year", today.replace(month=1, day=1), today)
    if re.search(r"all time|ever|overall|total", t):
        return Period("all time", date(2000, 1, 1), today)
    n = {"last 30 days": 30, "last 7 days": 7}.get(default, 30)
    return Period(f"the last {n} days", today - timedelta(days=n - 1), today)


def inr(value) -> str:
    """Indian grouping: 1234567.5 -> '₹12,34,567.50' (whole rupees drop the paise)."""
    v = Decimal(value or 0).quantize(Decimal("0.01"))
    whole, frac = divmod(abs(v), 1)
    digits = str(int(whole))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    sign = "-" if v < 0 else ""
    paise = f".{int(frac * 100):02d}" if frac else ""
    return f"{sign}₹{digits}{paise}"


def qty(value) -> str:
    v = Decimal(value or 0)
    return f"{v.normalize():f}" if v != v.to_integral_value() else str(int(v))
