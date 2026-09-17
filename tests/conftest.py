from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "test.db"
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{db_path}",
        redis_url="redis://localhost:6379/15",
        api_key="test-api-key",
        provider_ar_base_url="http://provider-ar.test",
        provider_br_base_url="http://provider-br.test",
        provider_ar_timeout_ms=100,
        provider_br_timeout_ms=100,
        provider_ar_max_retries=3,
        provider_br_max_retries=3,
        retry_base_ms=10,
        retry_cap_ms=50,
        idempotency_lock_ttl_seconds=30,
        pending_reconciliation_minutes=15,
        pending_reconciliation_poll_seconds=0,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    app = create_app(settings=settings, redis_client=fake_redis)
    with TestClient(app) as test_client:
        yield test_client
