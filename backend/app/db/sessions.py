from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import BrowserSession


class SessionRepository:
    """Session persistence; callers commit the complete operation."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_active(
        self, token_hash: str, now: datetime, *, lock: bool = False
    ) -> BrowserSession | None:
        statement = (
            select(BrowserSession)
            .where(BrowserSession.token_hash == token_hash, BrowserSession.expires_at > now)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def add(self, record: BrowserSession) -> None:
        self.session.add(record)
        self.session.flush()

    def remove(self, token_hash: str) -> None:
        self.session.execute(delete(BrowserSession).where(BrowserSession.token_hash == token_hash))

    def remove_expired(self, now: datetime) -> int:
        result = self.session.execute(
            delete(BrowserSession).where(BrowserSession.expires_at <= now)
        )
        return result.rowcount
