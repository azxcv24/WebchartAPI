"""데이터셋을 지울 때의 Redis 정리 테스트: 타일 키와 세션을 그 데이터셋 것만 지운다."""

import subprocess
import sys

import pytest

from webchart.cache import redis_cache
from webchart.core.errors import DatasetNotFoundError
from webchart.services.data.access import exclusive_update


async def test_purge_tiles_removes_only_that_dataset(fake_redis, monkeypatch):
    monkeypatch.setattr(redis_cache, "PURGE_BATCH_KEYS", 7)  # 배치 경계를 여러 번 넘게
    for i in range(30):
        await fake_redis.set(f"tile:gone:v1:abc:3-{i}@1x:e0", b"png")
    await fake_redis.set("tile:kept:v1:abc:0-0@1x:e0", b"png")
    await fake_redis.set("tile:gone-but-different:v1:abc:0-0@1x:e0", b"png")
    await fake_redis.set("session:x", b"{}")

    assert await redis_cache.purge_tiles("gone") == 30
    remaining = sorted(key.decode() for key in await fake_redis.keys("*"))
    assert remaining == [
        "session:x",
        "tile:gone-but-different:v1:abc:0-0@1x:e0",
        "tile:kept:v1:abc:0-0@1x:e0",
    ]


async def test_purge_tiles_treats_glob_characters_literally(fake_redis):
    await fake_redis.set("tile:abc:v1:0-0@1x:e0", b"png")
    assert await redis_cache.purge_tiles("a*") == 0
    assert await fake_redis.exists("tile:abc:v1:0-0@1x:e0")


async def test_sessions_are_indexed_by_dataset(fake_redis):
    await redis_cache.set_session("s1", {"dataset_id": "ds"})
    await redis_cache.set_session("s2", {"dataset_id": "ds"})
    await redis_cache.set_session("s3", {"dataset_id": "other"})
    await redis_cache.delete_session("s2", "ds")  # 닫은 세션은 색인에서도 빠진다
    assert await fake_redis.smembers("dataset_sessions:ds") == {b"s1"}
    assert await fake_redis.ttl("dataset_sessions:ds") > 0

    assert await redis_cache.delete_sessions_for_dataset("ds") == 1
    assert await redis_cache.get_session("s1") is None
    assert await redis_cache.get_session("s3") is not None
    assert not await fake_redis.exists("dataset_sessions:ds")


@pytest.fixture
def shared_redis(monkeypatch):
    """앱(TestClient의 이벤트 루프)과 테스트(동기 클라이언트)가 같은 가짜 Redis를 본다."""
    import fakeredis
    import fakeredis.aioredis

    server = fakeredis.FakeServer()
    monkeypatch.setattr(redis_cache, "_pool", fakeredis.aioredis.FakeRedis(server=server))
    return fakeredis.FakeRedis(server=server)


def test_delete_dataset_purges_tiles_and_sessions(shared_redis, client, sample_csv):
    import time

    with open(sample_csv, "rb") as f:
        dataset_id = client.post("/api/v1/datasets/upload", files={"file": f}).json()["dataset_id"]
    for _ in range(100):
        if client.get(f"/api/v1/datasets/{dataset_id}").json()["status"] == "ready":
            break
        time.sleep(0.05)
    shared_redis.set(f"tile:{dataset_id}:v1:abc:0-0@1x:e0", b"png")
    shared_redis.set("session:sess", f'{{"dataset_id": "{dataset_id}"}}')
    shared_redis.sadd(f"dataset_sessions:{dataset_id}", "sess")

    assert client.delete(f"/api/v1/datasets/{dataset_id}").status_code == 204

    assert shared_redis.keys("*") == []


def test_exclusive_update_blocks_other_processes(tmp_data_dir):
    dataset_dir = tmp_data_dir / "datasets" / "locked"
    dataset_dir.mkdir()
    probe = (
        "import fcntl, sys\n"
        "with open(sys.argv[1], 'a+b') as handle:\n"
        "    try:\n"
        "        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n"
        "        sys.exit(3)\n"
    )
    lock_file = str(dataset_dir / ".update.lock")
    with exclusive_update("locked"):
        held = subprocess.run([sys.executable, "-c", probe, lock_file])
    released = subprocess.run([sys.executable, "-c", probe, lock_file])
    assert (held.returncode, released.returncode) == (3, 0)


def test_exclusive_update_requires_existing_dataset():
    with pytest.raises(DatasetNotFoundError):
        with exclusive_update("missing"):
            pass
