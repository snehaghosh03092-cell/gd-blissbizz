from datetime import datetime

from sqlalchemy import case, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import RateLimitBucket


class RateLimitRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def consume(self, key_hash: str, expires_at: datetime, limit: int) -> bool:
        dialect = self.session.get_bind().dialect.name
        values = dict(key_hash=key_hash, expires_at=expires_at, attempt_count=1)
        count = RateLimitBucket.attempt_count
        next_count = case((count < limit + 1, count + 1), else_=count)
        # Atomic upserts are intentionally dialect-specific and isolated here.
        if dialect == "mysql":
            statement = (
                mysql_insert(RateLimitBucket)
                .values(**values)
                .on_duplicate_key_update(attempt_count=next_count)
            )
        elif dialect == "sqlite":
            statement = (
                sqlite_insert(RateLimitBucket)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[RateLimitBucket.key_hash], set_={"attempt_count": next_count}
                )
            )
        else:
            raise RuntimeError("Registration rate limiting supports MySQL and test SQLite only.")
        self.session.execute(statement)
        # The upsert retains its write lock until commit, including this read.
        attempts = self.session.scalar(select(count).where(RateLimitBucket.key_hash == key_hash))
        return attempts <= limit
