import hashlib
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ApiError
from app.db.rate_limits import RateLimitRepository


class RegistrationRateLimiter:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.clock = clock

    def bucket(self, peer: str) -> tuple[str, int]:
        window = self.settings.registration_rate_window_seconds
        start = int(self.clock()) // window * window
        key = hashlib.sha256(f"registration:{peer}:{start}".encode()).hexdigest()
        return key, start + window

    def enforce(self, peer: str) -> None:
        key, end = self.bucket(peer)
        with self.session_factory() as session, session.begin():
            allowed = RateLimitRepository(session).consume(
                key,
                datetime.fromtimestamp(end, UTC).replace(tzinfo=None),
                self.settings.registration_rate_limit,
            )
        # Keep quota consumption committed even when registration fails or is rejected.
        if not allowed:
            retry_after = max(1, math.ceil(end - self.clock()))
            raise ApiError(
                429,
                "rate_limited",
                "Too many registration attempts. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )
