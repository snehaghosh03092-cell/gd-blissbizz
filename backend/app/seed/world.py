"""Assembles the whole seed world: accounts, tenants, simulated activity, storefronts and platform data.

Returns ``{table: [row dicts]}``; ``loader.load`` inserts them. Nothing here touches the database.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from app.core.passwords import PasswordService
from app.seed.catalogs import ACTIVE_BUSINESSES, ALL_BUSINESSES, BusinessSpec
from app.seed.reference import DEV_PASSWORD, tax_rate_rows
from app.seed.simulate import IST, TenantSim, slugify

SEED_DEFAULT = 20261118
HOURS = {
    "fashion": ("10:00", "21:00", True),
    "grocery": ("07:00", "22:00", True),
    "electronics": ("10:00", "19:00", False),
    "gaming": ("11:00", "20:00", False),
    "food": ("10:00", "18:00", False),
}


def _uid(r: random.Random) -> str:
    return str(uuid.UUID(int=r.getrandbits(128), version=4))


def build_world(
    anchor: datetime, scale: float = 1.0, seed: int = SEED_DEFAULT
) -> dict[str, list[dict]]:
    r = random.Random(f"world:{seed}")
    rows: dict[str, list[dict]] = defaultdict(list)
    password_hash = PasswordService().hash(
        DEV_PASSWORD
    )  # one Argon2id hash reused by every dev account
    now = anchor

    def ago(days: float, hour: int = 10) -> datetime:
        return now - timedelta(days=days) + timedelta(hours=hour - (now + IST).hour)

    # ---- platform admin
    admin_id = _uid(r)
    rows["owners"].append(
        {
            "id": admin_id,
            "full_name": "Platform Admin",
            "email": "platform@blissbiz.example",
            "password_hash": password_hash,
            "phone": None,
            "platform_role": "admin",
            "disabled_at": None,
            "created_at": ago(300),
            "updated_at": ago(300),
        }
    )
    sims: list[TenantSim] = []
    for spec in ALL_BUSINESSES:
        owner_ids = {"owner": _uid(r), "manager": _uid(r), "staff": _uid(r)}
        business_id = _uid(r)
        created = ago(210 if spec.active else 3 if spec.key == "newleaf" else 120)
        people = [("owner", "Owner", "OWNER")] + (
            [("manager", "Manager", "MANAGER"), ("staff", "Staff", "STAFF")] if spec.active else []
        )
        for role_key, label, _member_role in people:
            rows["owners"].append(
                {
                    "id": owner_ids[role_key],
                    "full_name": f"{spec.name} {label}",
                    "email": f"{role_key}@{spec.key}.example",
                    "password_hash": password_hash,
                    "phone": f"+91 80000 {r.randint(10000, 99999)}",
                    "platform_role": None,
                    "disabled_at": None,
                    "created_at": created,
                    "updated_at": created,
                }
            )
        rows["businesses"].append(
            {
                "id": business_id,
                "owner_id": owner_ids["owner"],
                "name": spec.name,
                "slug": spec.key,
                "description": f"{spec.name}: {spec.category.lower()} from {spec.city}.",
                "contact_email": f"hello@{spec.key}.example",
                "contact_phone": spec.policy.get("support_phone", "+91 80000 00000"),
                "address": spec.address,
                "currency": "INR",
                "logo_path": None,
                "legal_name": spec.legal_name,
                "category": spec.category,
                "constitution": spec.constitution,
                "sales_mode": spec.sales_mode,
                "state_code": spec.state_code,
                "city": spec.city,
                "postal_code": spec.postal_code,
                "onboarding_state": spec.onboarding_state,
                "status": spec.status,
                "suspended_reason": "Seed example: repeated payment disputes under review"
                if spec.status == "SUSPENDED"
                else None,
                "created_at": created,
                "updated_at": created,
            }
        )
        for role_key, _label, member_role in people:
            rows["business_members"].append(
                {
                    "id": _uid(r),
                    "business_id": business_id,
                    "owner_id": owner_ids[role_key],
                    "role": member_role,
                    "invited_by": None if role_key == "owner" else owner_ids["owner"],
                    "created_at": created,
                }
            )
        rows["business_tax_profiles"].append(
            {
                "business_id": business_id,
                "gst_registered": spec.gst_registered,
                "gstin": spec.gstin,
                "gstin_state_code": spec.state_code if spec.gstin else None,
                "verification_status": "FORMAT_VALID" if spec.gstin else "UNVERIFIED",
                "verified_at": None,
                "verification_source": None,
                "taxpayer_scheme": "REGULAR" if spec.gst_registered else "UNKNOWN",
                "registration_state": spec.state_code if spec.gstin else None,
                "fy_start_month": 4,
                "invoice_prefix": spec.invoice_prefix,
                "prices_include_tax": spec.prices_include_tax,
                "updated_at": created,
            }
        )
        sim = TenantSim(spec, business_id, owner_ids, anchor, scale, seed, password_hash)
        sim.created = created
        sims.append(sim)
        for table, table_rows in sim.build().items():
            rows[table].extend(table_rows)
        _peripheral(rows, sim, r, ago)

    # ---- global platform data
    rows["tax_rate_versions"].extend(tax_rate_rows())
    _templates(rows, r, now)
    _integrations(rows, r, now, admin_id)
    _errors(rows, sims, now, r)
    _onboarding(rows, sims, r, now, ago)
    return rows


def _peripheral(rows, sim: TenantSim, r: random.Random, ago) -> None:
    spec, bid = sim.spec, sim.bid
    owner = sim.owner_ids["owner"]
    storefront_status = (
        "PUBLISHED" if spec.active else ("DRAFT" if spec.key == "newleaf" else "UNPUBLISHED")
    )
    featured = [v["product_id"] for v in sim.variants if v["persona"] == "BEST"]
    featured = list(dict.fromkeys(featured))[:6]
    categories = list(sim.categories) if spec.active else []
    config = {
        "hero": {"title": f"Welcome to {spec.name}", "subtitle": spec.category},
        "sections": ["hero", "featured", "categories", "about"],
        "navigation": categories[:8],
        "featured_product_ids": featured,
        "show_stock_badges": True,
    }
    rows["storefronts"].append(
        {
            "business_id": bid,
            "template_key": spec.template,
            "status": storefront_status,
            "theme": spec.theme,
            "draft_config": config,
            "published_config": config if storefront_status == "PUBLISHED" else None,
            "config_version": 3 if spec.active else 1,
            "published_at": ago(60) if spec.active else None,
            "unpublished_at": ago(10) if spec.status == "SUSPENDED" else None,
            "updated_at": ago(5),
        }
    )
    if spec.active or spec.status == "SUSPENDED":
        rows["domains"].append(
            {
                "id": _uid(r),
                "business_id": bid,
                "hostname": f"{spec.key}.localhost",
                "kind": "SUBDOMAIN",
                "status": "ACTIVE" if spec.active else "REMOVED",
                "tls_status": "NONE",
                "verification": None,
                "provider": "local",
                "provider_ref": None,
                "last_checked_at": ago(1),
                "error": None,
                "created_at": ago(60),
                "updated_at": ago(1),
            }
        )
    if spec.key == "threads-co":
        rows["domains"].append(
            {
                "id": _uid(r),
                "business_id": bid,
                "hostname": "shop.threadsandco.example",
                "kind": "CUSTOM",
                "status": "VERIFYING",
                "tls_status": "PENDING",
                "verification": {
                    "records": [
                        {"type": "A", "host": "@", "value": "203.0.113.10"},
                        {"type": "TXT", "host": "_blissbiz-challenge", "value": "bb-verify-7f3a91"},
                    ]
                },
                "provider": None,
                "provider_ref": None,
                "last_checked_at": ago(0.1),
                "error": "A record not found yet",
                "created_at": ago(2),
                "updated_at": ago(0.1),
            }
        )
    if not spec.active:
        return
    open_h, close_h, sunday = HOURS[spec.template]
    hours = {d: [open_h, close_h] for d in ("mon", "tue", "wed", "thu", "fri", "sat")}
    hours["sun"] = [open_h, close_h] if sunday else None
    p = spec.policy
    rows["store_policies"].append(
        {
            "business_id": bid,
            "timezone": "Asia/Kolkata",
            "support_hours": hours,
            "support_phone": p["support_phone"],
            "support_email": p["support_email"],
            "refund_window_days": p["refund_window_days"],
            "refund_method": p["refund_method"],
            "return_window_days": p["return_window_days"],
            "return_conditions": p["return_conditions"],
            "shipping_days_min": p["shipping_days_min"],
            "shipping_days_max": p["shipping_days_max"],
            "free_shipping_over": spec.free_shipping_over,
            "flat_shipping_fee": spec.flat_shipping,
            "warranty_days": p["warranty_days"],
            "cancel_window_hours": p["cancel_window_hours"],
            "show_exact_stock": spec.template == "grocery",
            "version": 1,
            "updated_at": ago(20),
        }
    )
    docs = [
        (
            "shipping",
            "Shipping policy",
            "PUBLIC",
            f"We ship across India. Delivery takes {p['shipping_days_min']} to {p['shipping_days_max']} days. "
            f"Orders above Rs {spec.free_shipping_over} ship free; otherwise a flat fee of Rs {spec.flat_shipping} applies.",
        ),
        (
            "returns",
            "Returns and refunds",
            "PUBLIC",
            f"{p['return_conditions']} Returns are accepted within {p['return_window_days']} days. "
            f"Refunds go to: {p['refund_method']}.",
        ),
        (
            "privacy",
            "Privacy notice",
            "PUBLIC",
            "We use your name, address and phone number only to deliver your order and send order updates.",
        ),
        (
            "owner-handbook",
            "Internal handbook",
            "OWNER",
            f"Staff discount is {10 + (len(spec.key) % 4) * 5} percent. Supplier payment terms are listed in the "
            "purchasing section. Do not share this document with customers.",
        ),
    ]
    for slug, title, audience, body in docs:
        rows["policy_documents"].append(
            {
                "id": _uid(r),
                "business_id": bid,
                "slug": slug,
                "title": title,
                "audience": audience,
                "body": body,
                "version": 1,
                "updated_at": ago(20),
            }
        )
    # ---- notifications derived from the simulated state
    low = [v for v in sim.variants if v["status"] == "PUBLISHED" and sim._avail(v) <= v["thr"]]
    for v in low[:10]:
        rows["notifications"].append(
            {
                "id": _uid(r),
                "business_id": bid,
                "kind": "LOW_STOCK",
                "payload": {
                    "variant_id": v["id"],
                    "sku": v["sku"],
                    "name": v["name"],
                    "available": str(sim._avail(v)),
                    "threshold": str(v["thr"]),
                },
                "read_at": None,
                "created_at": sim.now - timedelta(hours=r.randint(1, 20)),
            }
        )
    for order in sim.rows["orders"][-14:]:
        rows["notifications"].append(
            {
                "id": _uid(r),
                "business_id": bid,
                "kind": "ORDER_NEW",
                "payload": {
                    "order_id": order["id"],
                    "order_number": order["order_number"],
                    "total": str(order["grand_total"]),
                },
                "read_at": order["placed_at"] + timedelta(hours=3) if r.random() < 0.6 else None,
                "created_at": order["placed_at"],
            }
        )
    overdue = [
        po
        for po in sim.rows["purchase_orders"]
        if po["status"] == "APPROVED" and po["expected_date"] < sim.today
    ]
    for po in overdue[:4]:
        rows["notifications"].append(
            {
                "id": _uid(r),
                "business_id": bid,
                "kind": "PURCHASE_ORDER_OVERDUE",
                "payload": {"purchase_order_id": po["id"], "number": po["number"]},
                "read_at": None,
                "created_at": sim.now - timedelta(hours=r.randint(2, 30)),
            }
        )
    sample_orders = sim.rows["orders"][-12:]
    for i, order in enumerate(sample_orders):
        customer = next((c for c in sim.customers if c["id"] == order["customer_id"]), None)
        if customer is None:
            continue
        status, error = ("SUPPRESSED", "example domain: seed data must never receive real e-mail")
        if i % 6 == 4:
            status, error = ("FAILED", "SMTP is not configured (console provider only)")
        if i % 6 == 5:
            status, error = ("QUEUED", None)
        rows["message_deliveries"].append(
            {
                "business_id": bid,
                "notification_id": None,
                "channel": "EMAIL",
                "provider": "console",
                "recipient": customer["email"],
                "template_key": "order_confirmation",
                "subject": f"Order {order['order_number']} confirmed",
                "status": status,
                "attempts": 0 if status == "QUEUED" else 1,
                "last_error": error,
                "provider_message_id": None,
                "dedupe_key": f"order_confirmation:{order['id']}",
                "scheduled_at": order["placed_at"],
                "sent_at": None,
                "created_at": order["placed_at"],
            }
        )
    # ---- agent usage history (transcripts only)
    questions = [
        (
            "OWNER",
            "How many " + sim.variants[0]["name"].lower() + " do I have left?",
            "intent",
            38,
            0,
        ),
        ("OWNER", "Which products are running low?", "intent", 52, 0),
        ("OWNER", "What were my sales yesterday?", "intent", 61, 0),
        ("OWNER", "Which items are not selling?", "llm_fast", 1480, 1260),
        ("OWNER", "Compare revenue this month with last month", "llm_strong", 2210, 2890),
        ("SHOPPER", "show me black items under 1000 rupees", "intent", 44, 0),
        ("SHOPPER", "do you have size M", "llm_fast", 960, 780),
        ("SHOPPER", "what is your return policy", "policy_fact", 35, 0),
    ]
    for i, (role, text, route, latency, tokens) in enumerate(questions):
        rows["voice_logs"].append(
            {
                "business_id": bid,
                "role": role.lower(),
                "transcript": text,
                "route": route,
                "tool_calls": [],
                "prompt_tokens": tokens,
                "cached_tokens": tokens // 3,
                "completion_tokens": tokens // 10 if tokens else 0,
                "latency_ms": latency,
                "created_at": sim.now - timedelta(hours=2 * (i + 1)),
            }
        )
    if spec.key in ("threads-co", "freshbasket"):
        cid = _uid(r)
        rows["agent_conversations"].append(
            {
                "id": cid,
                "business_id": bid,
                "actor_type": "OWNER",
                "actor_id": owner,
                "audience": "OWNER",
                "created_at": sim.now - timedelta(hours=5),
            }
        )
        for j, (role, text) in enumerate(
            [
                ("USER", "Which products are running low?"),
                (
                    "ASSISTANT",
                    "Several items are at or below their reorder level; see the low stock list.",
                ),
            ]
        ):
            rows["agent_messages"].append(
                {
                    "conversation_id": cid,
                    "role": role,
                    "content": text,
                    "route": "intent" if role == "ASSISTANT" else None,
                    "tool_calls": None,
                    "prompt_tokens": 0,
                    "cached_tokens": 0,
                    "completion_tokens": 0,
                    "latency_ms": 45 if role == "ASSISTANT" else 0,
                    "created_at": sim.now - timedelta(hours=5) + timedelta(seconds=j * 3),
                }
            )
    # ---- audit trail samples
    for v in [x for x in sim.variants if x["persona"] in ("BEST", "SLOW")][:3]:
        rows["audit_events"].append(
            {
                "service": "catalog",
                "business_id": bid,
                "actor_type": "OWNER",
                "actor_id": owner,
                "action": "variant.price_changed",
                "entity_type": "product_variant",
                "entity_id": v["id"],
                "before": {"selling_price": str(round(float(v["price"]) * 0.95, 2))},
                "after": {"selling_price": str(v["price"])},
                "request_id": None,
                "created_at": ago(45),
            }
        )
    rows["audit_events"].append(
        {
            "service": "identity",
            "business_id": bid,
            "actor_type": "OWNER",
            "actor_id": owner,
            "action": "member.added",
            "entity_type": "business_member",
            "entity_id": sim.owner_ids["staff"],
            "before": None,
            "after": {"role": "STAFF"},
            "request_id": None,
            "created_at": ago(150),
        }
    )
    rows["audit_events"].append(
        {
            "service": "store",
            "business_id": bid,
            "actor_type": "OWNER",
            "actor_id": owner,
            "action": "storefront.published",
            "entity_type": "storefront",
            "entity_id": bid,
            "before": {"status": "DRAFT"},
            "after": {"status": "PUBLISHED", "config_version": 3},
            "request_id": None,
            "created_at": ago(60),
        }
    )
    rows["audit_events"].sort(key=lambda row: row["created_at"])


def _templates(rows, r, now) -> None:
    defaults = [
        (
            "welcome",
            "EMAIL",
            "Welcome to {{business_name}}",
            "Hi {{customer_name}}, thanks for creating an account.",
        ),
        (
            "order_confirmation",
            "EMAIL",
            "Order {{order_number}} confirmed",
            "Hi {{customer_name}}, we received your order {{order_number}} for Rs {{total}}.",
        ),
        (
            "order_confirmation",
            "SMS",
            None,
            "{{business_name}}: order {{order_number}} confirmed, Rs {{total}}.",
        ),
        (
            "order_shipped",
            "EMAIL",
            "Your order {{order_number}} has shipped",
            "Track it with {{carrier}} number {{tracking_number}}.",
        ),
        (
            "order_shipped",
            "SMS",
            None,
            "{{business_name}}: order {{order_number}} shipped via {{carrier}} {{tracking_number}}.",
        ),
        (
            "invoice_issued",
            "EMAIL",
            "Invoice {{invoice_number}}",
            "Your invoice {{invoice_number}} for order {{order_number}} is attached.",
        ),
        (
            "low_stock",
            "EMAIL",
            "Low stock: {{name}}",
            "{{name}} ({{sku}}) has {{available}} left; reorder level is {{threshold}}.",
        ),
        ("low_stock", "IN_APP", None, "{{name}} is low on stock ({{available}} left)."),
    ]
    for key, channel, subject, body in defaults:
        rows["notification_templates"].append(
            {
                "id": _uid(r),
                "business_id": None,
                "template_key": key,
                "channel": channel,
                "locale": "en",
                "subject": subject,
                "body": body,
                "active": True,
                "updated_at": now,
            }
        )


def _integrations(rows, r, now, admin_id) -> None:
    configs = [
        (
            "PAYMENT",
            "fake",
            {"mode": "local_sandbox", "note": "Real provider deferred"},
            None,
            "ENABLED",
        ),
        ("EMAIL", "console", {"note": "prints to logs; no delivery"}, None, "ENABLED"),
        (
            "EMAIL",
            "godaddy_smtp",
            {
                "host": "smtpout.secureserver.net",
                "port": 587,
                "starttls": True,
                "username_env": "GODADDY_SMTP_USER",
            },
            "GODADDY_SMTP_PASSWORD",
            "DISABLED",
        ),
        (
            "SMS",
            "console",
            {"note": "no GoDaddy SMS API found; provider undecided"},
            None,
            "ENABLED",
        ),
        ("STT", "browser", {"api": "Web Speech API"}, None, "ENABLED"),
        (
            "LLM",
            "anthropic",
            {"fast": "claude-haiku-5-5", "strong": "claude-sonnet-5-5"},
            "ANTHROPIC_API_KEY",
            "DISABLED",
        ),
        (
            "DEPLOY",
            "shared_tenant",
            {"note": "deployment deferred; local testing only"},
            None,
            "DISABLED",
        ),
        (
            "GST",
            "format_only",
            {"note": "syntax/state/checksum only; no official verification"},
            None,
            "ENABLED",
        ),
    ]
    for kind, provider, settings, secret, status in configs:
        rows["integration_configs"].append(
            {
                "id": _uid(r),
                "business_id": None,
                "provider_kind": kind,
                "provider": provider,
                "settings": settings,
                "secret_ref": secret,
                "status": status,
                "updated_by": admin_id,
                "updated_at": now,
            }
        )


def _errors(rows, sims, now, r) -> None:
    pixel = next((s for s in sims if s.spec.key == "pixelforge"), None)
    samples = [
        (
            pixel.bid if pixel else None,
            "payments.webhook",
            "invalid_signature",
            "Webhook rejected: bad signature (seed example)",
        ),
        (
            None,
            "notification.email",
            "provider_not_configured",
            "GoDaddy SMTP not configured; using console provider",
        ),
        (
            None,
            "notification.guard",
            "example_domain_suppressed",
            "Delivery to an example domain was suppressed",
        ),
        (
            None,
            "agent.llm",
            "llm_unavailable",
            "ANTHROPIC_API_KEY not set; falling back to intent shortcuts",
        ),
    ]
    for i, (bid, source, code, message) in enumerate(samples):
        rows["error_events"].append(
            {
                "service": source.split(".")[0],
                "business_id": bid,
                "source": source,
                "code": code,
                "message": message,
                "context": None,
                "request_id": None,
                "created_at": now - timedelta(hours=3 * (i + 1)),
                "resolved_at": now - timedelta(hours=1) if i == 3 else None,
            }
        )
    # forged webhook attempts (recorded, ignored)
    for i in range(3):
        rows["payment_webhook_events"].append(
            {
                "provider": "fake",
                "business_id": None,
                "event_id": f"evt_forged_{i}",
                "event_type": "payment.captured",
                "signature_valid": False,
                "payload": {"note": "seed example of a forged webhook"},
                "status": "IGNORED",
                "processed_at": None,
                "error": "signature mismatch",
                "received_at": now - timedelta(days=2 + i),
            }
        )


def _onboarding(rows, sims, r, now, ago) -> None:
    newleaf = next(s for s in sims if s.spec.key == "newleaf")

    def token(label: str) -> str:
        return hashlib.sha256(f"seed-draft-{label}".encode()).hexdigest()

    drafts = [
        (
            "anon-collecting",
            None,
            None,
            "NEW",
            "COLLECTING_DETAILS",
            {
                "business_name": "Green Leaf Bakery",
                "category": "Bakery",
                "city": "Bengaluru",
                "sales_mode": "ONLINE",
            },
            None,
            1,
        ),
        ("anon-explore", None, None, "EXPLORE", "DRAFT", {}, None, 5),
        (
            "owner-needs-correction",
            newleaf.owner_ids["owner"],
            newleaf.bid,
            "EXISTING",
            "NEEDS_CORRECTION",
            {"business_name": "NewLeaf Studio", "gst_registered": True, "gstin": "32AABCN1234P1Z9"},
            [
                {
                    "field": "tax.gstin",
                    "code": "bad_checksum",
                    "message": "This GSTIN has an invalid check character.",
                    "hint": "Re-enter the 15-character GSTIN from your certificate.",
                }
            ],
            2,
        ),
        ("expired", None, None, "NEW", "DRAFT", {"business_name": "Old idea"}, None, -3),
    ]
    for label, owner_id, business_id, intent, state, draft, errors, expires_in in drafts:
        draft_id = _uid(r)
        rows["onboarding_drafts"].append(
            {
                "id": draft_id,
                "token_hash": token(label),
                "owner_id": owner_id,
                "business_id": business_id,
                "intent": intent,
                "state": state,
                "draft": draft,
                "schema_version": 1,
                "validation_errors": errors,
                "expires_at": now + timedelta(days=expires_in),
                "created_at": ago(abs(expires_in) + 2),
                "updated_at": ago(1),
            }
        )
        if label == "anon-collecting":
            for j, (role, content) in enumerate(
                [
                    (
                        "ASSISTANT",
                        "Welcome to BlissBiz. Are you planning to start a new business, or do you already run one?",
                    ),
                    ("USER", "I want to open a small bakery in Bangalore"),
                    ("ASSISTANT", "Great. Will you sell online, in a shop, or both?"),
                    ("USER", "Mostly online at first"),
                ]
            ):
                rows["onboarding_messages"].append(
                    {
                        "draft_id": draft_id,
                        "role": role,
                        "content": content,
                        "patch": {"category": "Bakery", "city": "Bengaluru"} if j == 1 else None,
                        "created_at": ago(1) + timedelta(minutes=j),
                    }
                )


__all__ = ["build_world", "ACTIVE_BUSINESSES", "BusinessSpec", "slugify"]
