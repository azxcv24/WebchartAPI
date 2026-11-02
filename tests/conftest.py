"""Shared test fixtures."""

from pathlib import Path

import fakeredis.aioredis
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from webchart.config import settings


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    """Point settings.data_dir at a per-test temp dir. monkeypatch reverts."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "uploads").mkdir()
    (data_dir / "datasets").mkdir()
    monkeypatch.setattr(settings, "data_dir", data_dir)
    yield data_dir


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Use fakeredis in place of a real Redis server."""
    from webchart.cache import redis_cache

    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(redis_cache, "_pool", fake)

    async def _noop_init():
        return fake

    monkeypatch.setattr(redis_cache, "init_redis", _noop_init)
    yield fake


@pytest.fixture(autouse=True)
def paint_in_threads(monkeypatch):
    """테스트는 그리기 프로세스 풀 없이 렌더 스레드에서 그린다 (프로세스 기동 비용 회피)."""
    monkeypatch.setattr(settings, "paint_processes", 0)


@pytest.fixture(autouse=True)
def disable_rate_limit(monkeypatch):
    """Rate limit interferes with tight test loops; disable by default."""
    monkeypatch.setattr(settings, "rate_limit_enabled", False)


@pytest.fixture(autouse=True)
def disable_auth(monkeypatch):
    """Most tests assume auth off; opt-in via the require_auth fixture."""
    monkeypatch.setattr(settings, "api_keys", [])


@pytest.fixture(autouse=True)
def allow_local_egress(monkeypatch):
    """Tests hit localhost servers; disable the SSRF private-range guard."""
    monkeypatch.setattr(settings, "block_private_egress", False)


@pytest.fixture
def require_auth(monkeypatch):
    """Opt-in: enable API key auth for a single test."""
    monkeypatch.setattr(settings, "api_keys", ["test-key", "other-key"])
    return "test-key"


@pytest.fixture
def client():
    """FastAPI test client."""
    from webchart.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sample_csv(tmp_path) -> Path:
    """Create a sample CSV file for testing."""
    df = pd.DataFrame({
        "x": range(1000),
        "y": [i * 2.5 + (i % 7) for i in range(1000)],
        "category": [f"cat_{i % 5}" for i in range(1000)],
    })
    path = tmp_path / "sample.csv"
    df.to_csv(path, index=False)
    return path


@pytest.fixture
def sample_dataframe() -> pd.DataFrame:
    """Sample DataFrame for unit tests."""
    return pd.DataFrame({
        "x": range(10000),
        "y": [i * 1.5 + (i % 13) for i in range(10000)],
        "z": [i ** 0.5 for i in range(10000)],
    })
