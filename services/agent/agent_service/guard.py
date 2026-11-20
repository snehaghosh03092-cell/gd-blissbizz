"""Pre-LLM request guard (ported from the earlier voice agent). Deterministic and free.
Defence in depth only: tools still enforce role, tenant scope and read-only access.
check() returns a refusal (str) or None."""

import re

SHOPPER_REFUSAL = "I can help you find products, check availability, explain the shop's policies and show your orders."
PEOPLE_REFUSAL = "I can share counts and totals about customers, but not individual people's details."
_I = re.I
_LEAD = r"(?:^|\b(?:please|can you|could you|would you|go ahead and|i want you to|now|then)\s+)"
RULES = [
    (
        "injection",
        re.compile(
            r"ignore (all |any |the |your )?(previous|prior|above|earlier|system)?\s*(instructions|rules|prompts?)"
            r"|system prompt|developer (message|prompt)|reveal (your|the) (prompt|instructions)|jailbreak"
            r"|\byou are now\b|\bact as (an? )?(admin|root|owner|developer|dba)\b|pretend (to be|you are)"
            r"|disregard .*(rules|instructions)|\bdan mode\b|admin mode",
            _I,
        ),
        "I can't follow instructions that try to change my rules. Ask me about the shop instead.",
    ),
    (
        "sql",
        re.compile(
            r"\bselect\b.+\bfrom\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\bdelete\s+from\b|\bdrop\s+(table|database)\b"
            r"|\btruncate\s+table\b|\balter\s+table\b|\bunion\s+select\b|;\s*--|/\*|\bshow\s+tables\b|\binformation_schema\b",
            _I,
        ),
        "I don't run database commands. Ask a plain-language question instead.",
    ),
    (
        "secrets",
        re.compile(
            r"\b(\w*passwords?\w*|passwd|\w*_hash|secrets?|jwt|api[ -]?keys?|credentials?|tokens?|"
            r"home address|street address|credit card|card numbers?|cvv|aadhaar|pan number|bank account|login details)\b",
            _I,
        ),
        "I can't share credentials or personal details.",
    ),
    (
        "cross_tenant",
        re.compile(
            r"\b(other|another|different|competitor'?s?)\s+(stores?|shops?|tenants?|merchants?|businesses)\b",
            _I,
        ),
        "I can only answer about this shop.",
    ),
    (
        "write",
        re.compile(
            _LEAD + r"(delete|remove|drop|erase|wipe|destroy)\b"
            r"|"
            + _LEAD
            + r"(update|change|set|edit|modify|increase|decrease|raise|lower|reduce|cancel|refund|ship|approve)\b.*"
            r"\b(price|prices|stock|quantity|inventory|orders?|invoices?|products?|suppliers?|customers?)\b"
            r"|"
            + _LEAD
            + r"(add|create|insert|register|place)\b.*\b(products?|orders?|invoices?|customers?|suppliers?|stock)\b",
            _I,
        ),
        "I can look things up but not change data. Use the buttons in the app for changes.",
    ),
]
_PEOPLE = r"(users?|customers?|shoppers?|buyers?|staff|employees?|accounts?|people|members?)"
_INDIVIDUAL = re.compile(
    rf"\b(customer|user|account|shopper)\s*(number|no\.?|id|#)?\s*\d+\b|\bcustomer named\b|\bwho (bought|ordered|purchased)\b"
    rf"|\bnames? of (all |the |every )?{_PEOPLE}\b|\b(emails?|phone numbers?) of\b",
    _I,
)
_OWNER_ONLY = re.compile(
    r"\b(revenue|sales figures?|profit|margins?|cost price|purchase orders?|suppliers?|inventory value|turnover|"
    r"best ?sellers?|top[- ]selling|sign ?ups?|reorder\w*|overstock\w*|surplus|dead stock|dashboard|kpis?|gst collected)\b",
    _I,
)


def check(question: str, audience: str) -> str | None:
    q = question.strip()
    for name, rx, message in RULES:
        if rx.search(q):
            if (
                name == "write"
                and audience != "shopper"
                and re.search(r"\b(create|make)\s+(?:an?\s+)?(?:excel|xlsx|spreadsheet|workbook|export)\b", q, _I)
                and not re.search(r"\b(delete|remove|update|change|ship|refund|cancel)\b", q, _I)
            ):
                # Creating an export of existing records does not create business records.
                continue
            return SHOPPER_REFUSAL if audience == "shopper" and name in ("cross_tenant", "write") else message
    if _INDIVIDUAL.search(q):
        return SHOPPER_REFUSAL if audience == "shopper" else PEOPLE_REFUSAL
    if audience == "shopper" and _OWNER_ONLY.search(q):
        return SHOPPER_REFUSAL
    return None
