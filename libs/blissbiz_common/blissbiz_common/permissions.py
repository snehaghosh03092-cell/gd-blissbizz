"""Role -> permission table (docs/08 section 4). The gateway turns a verified membership into the
permissions carried in the signed context; services only check ``ctx.require_permission(...)``."""

OWNER_PERMISSIONS = frozenset({
    "business.read", "business.write", "team.manage", "tax.manage",
    "catalog.read", "catalog.write", "catalog.import",
    "inventory.read", "inventory.manage", "inventory.transfer", "inventory.adjust",
    "inventory.adjust.large", "inventory.reconcile",
    "purchasing.read", "purchasing.write", "purchasing.receive", "purchasing.approve",
    "sales.read", "sales.fulfil", "sales.refund", "sales.returns",
    "invoices.read", "invoices.adjust", "reports.read",
    "agent.use", "insights.read", "insights.act",
    "storefront.read", "storefront.write", "storefront.publish",
})  # fmt: skip

MANAGER_PERMISSIONS = OWNER_PERMISSIONS - {
    "business.write", "team.manage", "tax.manage", "inventory.adjust.large",
    "purchasing.approve", "invoices.adjust", "storefront.publish",
}  # fmt: skip

STAFF_PERMISSIONS = frozenset({
    "business.read", "catalog.read", "inventory.read", "purchasing.receive",
    "sales.read", "sales.fulfil", "invoices.read", "agent.use", "storefront.read",
})  # fmt: skip

ROLE_PERMISSIONS = {
    "OWNER": OWNER_PERMISSIONS,
    "MANAGER": MANAGER_PERMISSIONS,
    "STAFF": STAFF_PERMISSIONS,
}


def permissions_for(role: str | None) -> tuple[str, ...]:
    """Unknown or missing roles get nothing (fail closed)."""
    return tuple(sorted(ROLE_PERMISSIONS.get(role or "", frozenset())))
