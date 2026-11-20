"""Typed preparation tools. Artifact URLs are server-owned, never produced by model prose."""

import re
from datetime import date, timedelta
from urllib.parse import urlencode

from app.core.gstin import STATE_CODES
from blissbiz_common.errors import ApiError
from blissbiz_common.workbooks import workbook
from sqlalchemy import text

from agent_service.text import Period

INVOICE = re.compile(r"\b(invoices?|bills?|gst|tax)\b", re.I)
ACTION = re.compile(
    r"\b(collect|prepar\w*|export|download|excel|xlsx|spreadsheet|workbook|make|create|"
    r"compil\w*|gather|bundle|zip|give me|send me)\b",
    re.I,
)
REGISTRATION = re.compile(r"\b(registration|register|licen[cs]e|paperwork|documents?|docs|checklist)\b", re.I)
INVENTORY = re.compile(r"\b(inventory|stock|catalog|products?)\b", re.I)


def intent(question: str, history: list[dict]) -> str | None:
    """Direct action requests bypass general advice, including during provider outages.

    History resolves only action-only follow-ups; a new explicit subject always wins.
    """
    q = question.lower()
    if not ACTION.search(q):
        # An answer to the registration intake question refreshes that draft, without
        # changing the saved business or treating old advice as current user intent.
        for turn in reversed(history[-2:]):
            if turn["role"] == "assistant" and "registration preparation workbook" in turn["content"]:
                return "registration_pack" if registration_options([q]) else None
            if (
                turn["role"] == "assistant"
                and ("invoice downloads" in turn["content"] or "YYYY-MM-DD" in turn["content"])
                and re.fullmatch(
                    r"(?:for |from )?(?:\d{4}-\d{2}-\d{2}(?: (?:to|through) \d{4}-\d{2}-\d{2})?"
                    r"|(?:this|last|previous) (?:month|financial year|year)|all(?: invoices)?|today|yesterday)[.!?]*",
                    q.strip(),
                )
            ):
                return "invoice_export"
        return None
    if re.search(r"\b(invoices?|bills?)\b", q):
        return "invoice_export"
    if INVENTORY.search(q) and re.search(r"excel|xlsx|spreadsheet|export|download", q):
        return "inventory_export"
    if REGISTRATION.search(q) and re.search(r"gst|shop|business|registration|register", q):
        return "registration_pack"
    if INVOICE.search(q):
        return "invoice_export"
    # Do not reinterpret an explicit unrelated request from older invoice context.
    if re.fullmatch(
        r"(?:please\s+)?(?:yes[, ]+)?(?:prepare|collect|export|download|make|create|send|give)(?: me)? "
        r"(?:it|them|these|those|(?:the |an? )?(?:excel|spreadsheet|workbook)|the file)"
        r"(?: (?:as |in )?(?:an? )?(?:excel|spreadsheet|workbook))?(?: please)?[.!?]*",
        q.strip(),
    ):
        for turn in reversed(history):
            if turn["role"] == "user":
                prior = turn["content"].lower()
                if re.search(r"\binvoices?|\bbills?", prior):
                    return "invoice_export"
                if REGISTRATION.search(prior):
                    return "registration_pack"
                if INVENTORY.search(prior):
                    return "inventory_export"
    return None


def registration_options(messages: list[str]) -> dict:
    """Only non-sensitive declared intake fields, with later statements taking precedence."""
    values = {}
    for message in messages:
        q = message.lower()
        states = [code for code, name in STATE_CODES.items() if re.search(r"\b" + re.escape(name.lower()) + r"\b", q)]
        if len(states) == 1:
            values["state_code"] = states[0]
        for rx, value in (
            (r"\b(proprietor(?:ship)?|sole trader)\b", "PROPRIETORSHIP"),
            (r"\bpartnership\b", "PARTNERSHIP"),
            (r"\bllp\b", "LLP"),
            (r"\b(private limited|pvt ltd)\b", "PRIVATE_LIMITED"),
        ):
            if re.search(rx, q):
                values["constitution"] = value
        for rx, value in (
            (r"\b(owned|own premises|own property)\b", "OWNED"),
            (r"\b(rented|rent|leased)\b", "RENTED"),
            (r"\b(shared|consent)\b", "CONSENT"),
        ):
            if re.search(rx, q):
                values["premises"] = value
    return values


def requested_period(question: str, today: date) -> Period | None:
    """Explicit ranges, calendar months and financial years. Never silently ignore date text."""
    q = question.lower()
    raw = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", q)
    if raw:
        try:
            start, end = [date.fromisoformat(v) for v in raw] if len(raw) == 2 else (date.fromisoformat(raw[0]),) * 2
        except ValueError:
            raise ApiError(422, "invalid_period", "Use valid ISO dates: YYYY-MM-DD.") from None
        if len(raw) > 2 or start > end or start < date(1900, 1, 1) or end > date(9998, 12, 31):
            raise ApiError(422, "invalid_period", "Choose a valid start and end date in that order.")
        return Period(f"{start} through {end}", start, end)
    if "last month" in q or "previous month" in q:
        end = today.replace(day=1) - timedelta(days=1)
        return Period("last calendar month", end.replace(day=1), end)
    if "this month" in q:
        return Period("this month to date", today.replace(day=1), today)
    if "financial year" in q or re.search(r"\bfy\b", q):
        if re.search(r"\d", q):
            raise ApiError(
                422, "invalid_period", "For a specific financial year, provide its start and end as YYYY-MM-DD dates."
            )
        start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
        end = today
        if "last" in q or "previous" in q:
            end = start - timedelta(days=1)
            start = date(start.year - 1, 4, 1)
        return Period(f"{start} through {end}", start, end)
    if "yesterday" in q:
        return Period("yesterday", today - timedelta(days=1), today - timedelta(days=1))
    if "today" in q:
        return Period("today", today, today)
    days = re.search(r"(?:last|past)\s+(\d+)\s+days?", q)
    if days:
        n = int(days[1])
        if not 1 <= n <= 3660:
            raise ApiError(422, "invalid_period", "Choose between 1 and 3660 days, or explicit dates.")
        return Period(f"last {n} days", today - timedelta(days=n - 1), today)
    if "this year" in q:
        return Period("this calendar year to date", today.replace(month=1, day=1), today)
    if re.search(
        r"\b\d{4}\b|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|week|quarter|month|year)\b",
        q,
    ):
        raise ApiError(
            422,
            "invalid_period",
            "Please give the period as two dates (YYYY-MM-DD to YYYY-MM-DD), "
            "or say this month, last month or all invoices.",
        )
    return None


def artifacts(ctx, kind, period=None, options=None):
    ctx.require_permission(
        {"invoice_export": "invoices.read", "inventory_export": "inventory.read", "registration_pack": "business.read"}[
            kind
        ]
    )
    files = {
        "invoice_export": [
            ("Invoice register · Excel", "invoices.xlsx"),
            ("Invoice copies + register · ZIP", "invoices.zip"),
        ],
        "inventory_export": [("Inventory snapshot · Excel", "inventory.xlsx")],
        "registration_pack": [("Registration preparation · Excel", "registration.xlsx")],
    }[kind]
    params = {"start": period.start.isoformat(), "end": period.end.isoformat()} if period else (options or {})
    query = "?" + urlencode(params) if params else ""
    return [
        {
            "label": label,
            "filename": filename,
            "url": f"/api/v1/admin/businesses/{ctx.business_id}/documents/{filename}{query}",
        }
        for label, filename in files
    ]


def reply(env, question, kind, history=None):
    from agent_service.assistant import Reply

    if not env.b:
        return Reply(
            "Create or select your shop first so I can prepare a file from its"
            " saved details. Which state/UT, legal structure and premises "
            "arrangement will you use?",
            kind,
        )
    history = history or []
    options = (
        registration_options([turn["content"] for turn in history if turn["role"] == "user"] + [question])
        if kind == "registration_pack"
        else {}
    )
    per = requested_period(question, env.today) if kind == "invoice_export" else None
    if kind == "invoice_export" and not re.search(
        r"invoices?|bills?|gst|tax|\d|today|yesterday|month|year|week|quarter|all", question, re.I
    ):
        # A format-only follow-up retains the last invoice request's period.
        for turn in reversed(history):
            if turn["role"] == "user" and intent(turn["content"], []) == "invoice_export":
                per = requested_period(turn["content"], env.today)
                break
    downloads = artifacts(env.ctx, kind, per, options)
    if kind == "invoice_export":
        answer = (
            f"Your invoice downloads are ready for **{per.label if per else 'all issued invoices'}**. "
            "The Excel file includes invoice totals, line items, tax components and linked credit notes. "
            "The ZIP adds printable HTML copies and JSON records; open a copy to print or save as PDF.\n\n"
            "Only existing issued sales documents are included. Supplier purchase invoices are not included. "
            "The files are generated from current records when you download them. "
            "This is accounting support, not a GST return upload or a complete registration application. "
            "For a narrower period, give start and end dates (YYYY-MM-DD)."
        )
    elif kind == "inventory_export":
        answer = (
            "Download the current inventory workbook below. It includes all "
            "product variants, stock on hand, reservations, available "
            "quantities, prices and stock value across warehouses. The "
            "snapshot is generated when downloaded."
        )
    else:
        answer = (
            "Download your **registration preparation workbook** below. It fills in your saved business details "
            "and provides a GST evidence checklist, a shop-registration intake sheet and official sources. "
            "Missing fields and unverified supporting documents are marked clearly.\n\n"
            "Confirm your **state/UT, legal structure and whether the premises are owned, rented or shared** "
            "so the checklist can be tailored. Sales invoices are separate accounting records; they do not replace "
            "identity, constitution or premises evidence. I have not collected or verified those proofs or filed "
            "an application. Supply sensitive proofs directly through official channels."
        )
        if options:
            answer += (
                "\n\nIncluded your declared details in this draft: "
                + ", ".join(STATE_CODES.get(value, value.replace("_", " ").title()) for value in options.values())
                + ". These do not change the saved shop profile; state-specific forms still need official review."
            )
    return Reply(answer, kind, artifacts=downloads)


def inventory_workbook(db, ctx):
    ctx.require_permission("inventory.read")
    rows = db.execute(
        text(
            "SELECT product_name, sku, category, product_status, is_active, "
            "stock_unit, on_hand, reserved, available, price, cost_price, "
            "stock_value FROM rpt_inventory_current WHERE business_id=:b ORDER"
            " BY product_name, sku LIMIT 50001"
        ),
        {"b": ctx.business_id},
    ).all()
    if len(rows) > 50000:
        raise ApiError(422, "export_too_large", "More than 50,000 variants. This export is too large.")
    return workbook(
        [
            (
                "Inventory",
                [
                    [
                        "Product",
                        "SKU",
                        "Category",
                        "Status",
                        "Active",
                        "Unit",
                        "On hand",
                        "Reserved",
                        "Available",
                        "Price INR",
                        "Cost INR",
                        "Stock value INR",
                    ],
                    *[list(row) for row in rows],
                ],
            ),
            (
                "Coverage",
                [
                    ["Topic", "Value"],
                    [
                        "Scope",
                        "All variants, including drafts and inactive variants. Quantities "
                        "summed across warehouses. Available = on hand minus reserved; "
                        "incoming purchases excluded.",
                    ],
                    ["Timing", "Current snapshot at download time; no historical date filter."],
                    [
                        "Stock value",
                        "Stored weighted-average stock value; not available units multiplied by list cost.",
                    ],
                ],
            ),
        ]
    )
