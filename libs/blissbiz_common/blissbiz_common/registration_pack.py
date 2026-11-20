"""A preparation worksheet, never proof of eligibility, document verification or a filing.

Requirements are conditional; the user supplies evidence directly to the relevant authority.
"""

from datetime import UTC, datetime

from blissbiz_common.workbooks import workbook

GST_MANUAL = (
    "https://tutorial.gst.gov.in/userguide/registration/Apply_for_Registration_Normal_Taxpayer.htm"
)
GST_RULES = "https://cbic-gst.gov.in/gst-registration-rules.html"
REVIEWED = "2026-11-20"

# Broad intake only. Do not imply that every item applies to every applicant.
REQUIREMENTS = [
    ("GST", "PAN and legal name", "Business PAN; proprietor uses own PAN", GST_MANUAL),
    (
        "GST",
        "Contact verification",
        "Accessible signatory email and Indian mobile for portal OTP",
        GST_MANUAL,
    ),
    (
        "GST",
        "People and authentication",
        "Promoter/signatory details, photos and portal authentication as applicable",
        GST_MANUAL,
    ),
    (
        "GST",
        "Constitution evidence",
        "Entity-dependent: partnership deed or applicable constitution/registration evidence",
        GST_RULES,
    ),
    (
        "GST",
        "Authorised signatory",
        "Applicable authorisation/appointment evidence; confirm for the entity",
        GST_RULES,
    ),
    ("GST", "Owned premises", "Ownership evidence accepted by the current portal", GST_RULES),
    (
        "GST",
        "Rented/leased premises",
        "Rent/lease and lessor ownership evidence as applicable",
        GST_RULES,
    ),
    (
        "GST",
        "Consent/shared premises",
        "Consent and ownership evidence as applicable; confirm current category",
        GST_RULES,
    ),
    (
        "GST",
        "Business activities",
        "Goods/services, relevant HSN/SAC, commencement and reason for registration",
        GST_MANUAL,
    ),
    (
        "GST",
        "Bank evidence",
        "Not mandatory at initial application; complete later as required by the portal",
        GST_MANUAL,
    ),
]


def registration_workbook(profile: dict, declared: dict | None = None) -> bytes:
    declared = declared or {}
    fields = {**profile.get("fields", {}), **declared}
    state = (profile.get("states") or {}).get(fields.get("state_code")) or fields.get("state_code")
    details = [
        ["Field", "Saved value", "Evidence status"],
        [
            "Prepared UTC",
            datetime.now(UTC).isoformat(timespec="seconds"),
            "Snapshot when downloaded",
        ],
        ["Purpose", "GST and shop registration preparation", "Draft; no application submitted"],
    ]
    for key, label in (
        ("name", "Trading name"),
        ("legal_name", "Legal name"),
        ("constitution", "Legal structure"),
        ("state_code", "State/UT"),
        ("city", "City"),
        ("postal_code", "PIN code"),
        ("address", "Business address"),
        ("contact_email", "Business email"),
        ("contact_phone", "Business phone"),
        ("category", "Activity"),
        ("gstin", "Saved GSTIN"),
    ):
        value = state if key == "state_code" else fields.get(key)
        details.append(
            [
                label,
                value or "To provide",
                "Declared in chat; not verified"
                if key in declared
                else "Saved text; supporting proof not checked"
                if value
                else "Missing",
            ]
        )
    checklist = [
        ["Registration", "Item", "Applicability / next step", "Evidence status", "Official source"]
    ]
    premises_items = {
        "Owned premises": "OWNED",
        "Rented/leased premises": "RENTED",
        "Consent/shared premises": "CONSENT",
    }
    for kind, item, explanation, source in REQUIREMENTS:
        status = "Not collected or verified"
        if item in premises_items and fields.get("premises"):
            if premises_items[item] != fields["premises"]:
                status = "Not selected for declared premises"
        if item == "Constitution evidence" and fields.get("constitution") == "PROPRIETORSHIP":
            explanation = (
                "Proprietorship declared: partnership deed/company papers not"
                " selected; confirm portal evidence."
            )
        checklist.append([kind, item, explanation, status, source])
    shop = [
        ["Shop registration intake", "Value / next step", "Status"],
        ["State/UT", state or "Which state/UT is the shop in?", "Confirm local jurisdiction"],
        [
            "Legal structure",
            fields.get("constitution") or "Proprietor, partnership, LLP, company or other?",
            "Confirm",
        ],
        [
            "Premises",
            fields.get("premises") or "Owned, rented/leased, consent/shared or other?",
            "Declared; not verified" if fields.get("premises") else "To provide",
        ],
        ["Workforce", "Number of workers and nature of establishment", "To provide"],
        ["Dates", "Commencement date and operating hours", "To provide"],
        [
            "Local authority",
            "Identify the official state labour / municipal portal before finalising requirements",
            "Pending state-specific review",
        ],
        [
            "Evidence",
            (
                "Confirm applicable identity, constitution, premises and "
                "workforce documents on that portal"
            ),
            "Provisional; not a legal checklist",
        ],
    ]
    notes = [
        ["Topic", "Explanation"],
        [
            "Use",
            (
                "Preparation worksheet for owner/adviser review; not a "
                "government form, certificate or filed application."
            ),
        ],
        [
            "Evidence",
            (
                "The app has profile text, not verified identity or premises "
                "documents. No supporting proofs are included."
            ),
        ],
        [
            "Sales invoices",
            (
                "Order invoices are accounting records; do not treat this "
                "export as a complete registration application."
            ),
        ],
        [
            "Missing details",
            (
                "Confirm state/UT, entity type and premises arrangement "
                "before tailoring the registration pack."
            ),
        ],
        [
            "Privacy",
            (
                "Provide sensitive identity proofs directly through official "
                "channels, not in this chat."
            ),
        ],
        [
            "GST source review",
            REVIEWED + "; confirm current portal requirements before submitting.",
        ],
        ["GST application guide", GST_MANUAL],
        ["GST registration rules", GST_RULES],
        [
            "Shop registration",
            (
                "Requirements and forms depend on the state and establishment"
                "; no fees or deadlines assumed."
            ),
        ],
    ]
    return workbook(
        [
            ("Business draft", details),
            ("GST checklist", checklist),
            ("Shop intake", shop),
            ("Notes and sources", notes),
        ]
    )
