import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.errors import ApiError
from app.db.models import RateLimitBucket
from app.db.rate_limits import RateLimitRepository
from app.db.session import RateLimitSessionLocal
from app.services.rate_limits import RegistrationRateLimiter


def test_atomic_bucket_limit_on_database(db):
    repository = RateLimitRepository(db)
    key = uuid4().hex.ljust(64, "0")
    end = datetime(2030, 1, 1)
    assert repository.consume(key, end, 2)
    assert repository.consume(key, end, 2)
    assert not repository.consume(key, end, 2)
    assert not repository.consume(key, end, 2)


def test_window_reset_and_distinct_clients(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'limits.sqlite'}")
    RateLimitBucket.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    config = Settings(_env_file=None, database_url="sqlite://", registration_rate_limit=1)
    now = [1800.0]
    limiter = RegistrationRateLimiter(factory, config, clock=lambda: now[0])
    try:
        limiter.enforce("client-a")
        with pytest.raises(ApiError) as error:
            limiter.enforce("client-a")
        assert error.value.status_code == 429
        limiter.enforce("client-b")
        now[0] = 2700.0
        limiter.enforce("client-a")
    finally:
        engine.dispose()


def test_workers_share_atomic_quota(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent-limits.sqlite'}")
    RateLimitBucket.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    config = Settings(_env_file=None, database_url="sqlite://", registration_rate_limit=3)

    def attempt(_):
        limiter = RegistrationRateLimiter(factory, config, clock=lambda: 1800.0)
        try:
            limiter.enforce("shared-peer")
            return True
        except ApiError as error:
            assert error.status_code == 429
            return False

    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            assert sum(workers.map(attempt, range(8))) == 3
    finally:
        engine.dispose()


@pytest.mark.skipif(
    os.getenv("BLISSBIZ_TEST_MYSQL") != "1", reason="Opt-in local MySQL concurrency test"
)
def test_mysql_workers_share_committed_quota():
    config = Settings(_env_file=None, database_url="sqlite://", registration_rate_limit=3)
    peer = f"test-{uuid4().hex}"
    limiter = RegistrationRateLimiter(RateLimitSessionLocal, config, clock=lambda: 1800.0)
    key, _ = limiter.bucket(peer)

    def attempt(_):
        try:
            RegistrationRateLimiter(RateLimitSessionLocal, config, clock=lambda: 1800.0).enforce(
                peer
            )
            return True
        except ApiError as error:
            assert error.status_code == 429
            return False

    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            assert sum(workers.map(attempt, range(8))) == 3
    finally:
        # Only remove this test's uniquely identified security counter, never business records.
        with RateLimitSessionLocal() as session, session.begin():
            session.execute(delete(RateLimitBucket).where(RateLimitBucket.key_hash == key))
