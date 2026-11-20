"""The identity a service trusts: built by the gateway from a verified session and carried
in a signed header.

A service never trusts a business or user id from a request body; it compares it with this context.
"""

import base64
import json
from dataclasses import asdict, dataclass, field

from blissbiz_common.errors import ApiError


@dataclass(frozen=True)
class InternalContext:
    request_id: str
    actor_type: str  # OWNER | CUSTOMER | PLATFORM_ADMIN | SERVICE | ANONYMOUS
    business_id: str | None = None
    actor_id: str | None = None
    role: str | None = None
    permissions: tuple[str, ...] = field(default_factory=tuple)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def require_business(self, business_id: str) -> str:
        """Selector check: an id in a path or body must equal the proven business (else 404)."""
        if self.business_id is None or business_id != self.business_id:
            raise ApiError(404, "not_found", "Not found.")
        return self.business_id

    def require_permission(self, permission: str) -> None:
        if not self.has(permission):
            raise ApiError(403, "forbidden", "You do not have permission to do that.")


def encode_context(ctx: InternalContext) -> str:
    raw = json.dumps(asdict(ctx), sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_context(value: str) -> InternalContext:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw)
        data["permissions"] = tuple(data.get("permissions", ()))
        return InternalContext(**data)
    except Exception as exc:  # malformed context is an authentication failure, not a 500
        raise ApiError(401, "invalid_internal_request", "Invalid internal request.") from exc
