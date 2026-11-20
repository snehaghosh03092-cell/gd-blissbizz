import hashlib
import math
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.config import Settings
from app.core.errors import ApiError, csrf_rejected
from app.db.models import BrowserSession, utc_now
from app.db.sessions import SessionRepository


@dataclass(frozen=True)
class SessionCredentials:
    raw_token: str = field(repr=False)
    record: BrowserSession = field(repr=False)


class SessionService:
    TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")

    def __init__(
        self,
        repository: SessionRepository,
        settings: Settings,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock

    @staticmethod
    def token_hash(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("ascii")).hexdigest()

    def current(self, raw_token: str | None, *, lock: bool = False) -> BrowserSession | None:
        if raw_token is None or self.TOKEN_PATTERN.fullmatch(raw_token) is None:
            return None
        return self.repository.find_active(self.token_hash(raw_token), self.clock(), lock=lock)

    def bootstrap(self, raw_token: str | None) -> SessionCredentials:
        current = self.current(raw_token)
        if current is not None:
            return SessionCredentials(raw_token=raw_token, record=current)
        if raw_token is not None and self.TOKEN_PATTERN.fullmatch(raw_token):
            self.repository.remove(self.token_hash(raw_token))
        return self._issue(owner_id=None)

    def _issue(self, owner_id: str | None) -> SessionCredentials:
        raw_token = secrets.token_urlsafe(32)
        lifetime = (
            self.settings.anonymous_session_ttl_seconds
            if owner_id is None
            else self.settings.owner_session_ttl_seconds
        )
        now = self.clock()
        record = BrowserSession(
            token_hash=self.token_hash(raw_token),
            csrf_token=secrets.token_urlsafe(32),
            owner_id=owner_id,
            created_at=now,
            expires_at=now + timedelta(seconds=lifetime),
        )
        self.repository.add(record)
        return SessionCredentials(raw_token=raw_token, record=record)

    def validate_csrf(self, raw_token: str | None, csrf_token: str | None) -> BrowserSession:
        record = self.current(raw_token)
        if (
            record is None
            or csrf_token is None
            or self.TOKEN_PATTERN.fullmatch(csrf_token) is None
            or not secrets.compare_digest(record.csrf_token, csrf_token)
        ):
            raise csrf_rejected()
        return record

    def require_owner(self, raw_token: str | None) -> BrowserSession:
        record = self.current(raw_token)
        if record is None or record.owner_id is None:
            raise ApiError(401, "authentication_required", "A valid owner session is required.")
        return record

    def rotate(self, raw_token: str, owner_id: str | None) -> SessionCredentials:
        """Call only after CSRF/credentials checks; commit with registration/login writes."""
        current = self.current(raw_token, lock=True)
        if current is None:
            raise ApiError(401, "authentication_required", "A valid session is required.")
        self.repository.remove(current.token_hash)
        return self._issue(owner_id=owner_id)

    def revoke(self, raw_token: str | None) -> None:
        if raw_token is not None and self.TOKEN_PATTERN.fullmatch(raw_token):
            self.repository.remove(self.token_hash(raw_token))

    def remaining_seconds(self, record: BrowserSession) -> int:
        return max(0, math.ceil((record.expires_at - self.clock()).total_seconds()))

    def purge_expired(self) -> int:
        """Maintenance helper, not a public endpoint. Caller commits."""
        return self.repository.remove_expired(self.clock())
