"""HMAC-signed internal requests (docs/02 section 4.1, docs/08 section 12).

Signature = HMAC-SHA256 over: METHOD, path?query, timestamp, nonce, sha256(body), context
(newline separated). Rejected: wrong or missing signature, tampered body/path/context,
timestamps outside +-60 s, and replayed nonces.
"""

import hashlib
import hmac
import secrets
import threading
import time

from blissbiz_common.context import InternalContext, decode_context, encode_context
from blissbiz_common.errors import ApiError

SIGNATURE = "X-Internal-Signature"
TIMESTAMP = "X-Internal-Timestamp"
NONCE = "X-Internal-Nonce"
CONTEXT = "X-Internal-Context"
WINDOW_SECONDS = 60


def _canonical(
    method: str, target: str, timestamp: str, nonce: str, body: bytes, context: str
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), target, timestamp, nonce, body_hash, context]).encode()


def _mac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign_request(
    secret: str,
    method: str,
    target: str,
    body: bytes,
    ctx: InternalContext,
    *,
    now: float | None = None,
) -> dict[str, str]:
    """Headers to attach to an internal call. ``target`` is the path plus query string."""
    if len(secret) < 32:
        raise ValueError("signing secret must be at least 32 characters")
    timestamp = str(int(now if now is not None else time.time()))
    nonce = secrets.token_hex(16)
    context = encode_context(ctx)
    signature = _mac(secret, _canonical(method, target, timestamp, nonce, body, context))
    return {SIGNATURE: signature, TIMESTAMP: timestamp, NONCE: nonce, CONTEXT: context}


class NonceCache:
    """Remembers recent nonces so a captured request cannot be replayed inside the time window."""

    def __init__(self, ttl: int = WINDOW_SECONDS * 2):
        self._ttl, self._seen, self._lock = ttl, {}, threading.Lock()

    def check_and_add(self, nonce: str, now: float) -> bool:
        with self._lock:
            self._seen = {n: t for n, t in self._seen.items() if now - t < self._ttl}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


def verify_request(
    secret: str,
    method: str,
    target: str,
    body: bytes,
    headers: dict[str, str],
    nonces: NonceCache,
    *,
    now: float | None = None,
) -> InternalContext:
    """Return the trusted context or raise ApiError(401). Messages are deliberately uniform."""
    fail = ApiError(401, "invalid_internal_request", "Invalid internal request.")
    if len(secret) < 32:
        raise ApiError(503, "service_misconfigured", "Internal signing is not configured.")
    lowered = {k.lower(): v for k, v in headers.items()}
    signature, timestamp = lowered.get(SIGNATURE.lower()), lowered.get(TIMESTAMP.lower())
    nonce, context = lowered.get(NONCE.lower()), lowered.get(CONTEXT.lower())
    if not (signature and timestamp and nonce and context):
        raise fail
    current = now if now is not None else time.time()
    try:
        if abs(current - int(timestamp)) > WINDOW_SECONDS:
            raise fail
    except ValueError:
        raise fail from None
    expected = _mac(secret, _canonical(method, target, timestamp, nonce, body, context))
    if not hmac.compare_digest(expected, signature):
        raise fail
    if not nonces.check_and_add(nonce, current):  # only after the signature is proven valid
        raise fail
    return decode_context(context)
