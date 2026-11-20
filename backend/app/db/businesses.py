from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Business


class BusinessRepository:
    """Database operations; callers own transaction boundaries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def slug_exists(self, slug: str) -> bool:
        statement = select(Business.id).where(Business.slug == slug).limit(1)
        return self.session.scalar(statement) is not None

    def add(self, business: Business) -> None:
        self.session.add(business)
        self.session.flush()
