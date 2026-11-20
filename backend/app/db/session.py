from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url.get_secret_value(),
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5},
)
SessionLocal = sessionmaker(bind=engine)

# Quota commits happen while a request holds its main DB connection. A separate
# bounded pool prevents nested checkouts from exhausting the main request pool.
rate_limit_engine = create_engine(
    settings.database_url.get_secret_value(),
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=3,
    connect_args={"connect_timeout": 5},
)
RateLimitSessionLocal = sessionmaker(bind=rate_limit_engine)


# Explicit names keep MySQL/SQLite constraint names identical and Alembic diffs stable.
# (Primary keys are intentionally left unnamed, as in the existing migrations.)
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
