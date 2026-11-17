"""타일과 세션을 위한 Redis 캐시 계층.

연결 풀은 프로세스당 하나이며 lifespan에서 만들고 닫는다. 값은 바이너리(bytes)로
주고받는다 (타일 이미지 때문에 decode_responses=False).
"""

import asyncio
import json
import logging
from typing import Any

import redis.asyncio as redis

from webchart.config import settings

logger = logging.getLogger("webchart.cache")

_pool: redis.Redis | None = None
_init_lock = asyncio.Lock()


async def init_redis() -> redis.Redis:
    """Redis 연결 풀을 미리 만든다 (FastAPI lifespan에서 호출)."""
    global _pool
    async with _init_lock:
        if _pool is None:
            _pool = redis.from_url(
                settings.redis_url,
                decode_responses=False,  # 타일 이미지는 바이너리다
            )
    return _pool


async def get_redis() -> redis.Redis:
    """Redis 연결 풀을 돌려준다. 아직 없으면 락을 잡고 만든다."""
    if _pool is not None:
        return _pool
    return await init_redis()


async def close_redis() -> None:
    """Redis 연결 풀을 닫는다."""
    global _pool
    async with _init_lock:
        if _pool is not None:
            # redis-py >= 5.0.1에서는 close()가 deprecated이고 aclose()를 쓴다.
            closer = getattr(_pool, "aclose", None)
            if closer is not None:
                await closer()
            else:  # pragma: no cover - 구버전 redis-py용
                await _pool.close()
            _pool = None


# --- 타일 캐시 ---

# 키 형식은 application/tiles.py의 TileRequest.cache_key가 정한다.
# 모두 "tile:{dataset_id}:"로 시작하므로 데이터셋 단위로 지울 수 있다.

PURGE_BATCH_KEYS = 500


async def get_tile(key: str) -> bytes | None:
    r = await get_redis()
    return await r.get(key)


async def set_tile(key: str, data: bytes, ttl: int | None = None) -> None:
    """타일을 저장한다. ttl(초)을 주지 않으면 설정의 tile_cache_ttl을 쓴다."""
    r = await get_redis()
    await r.set(key, data, ex=ttl or settings.tile_cache_ttl)


async def purge_tiles(dataset_id: str) -> int:
    """데이터셋의 타일 키를 모두 지우고 지운 개수를 돌려준다 (데이터셋 삭제 뒤 백그라운드).

    SCAN은 조금씩 끊어 훑으므로 Redis를 오래 막지 않는다. UNLINK는 메모리 해제를 Redis의
    백그라운드 스레드에 맡긴다. 타일이 큰 바이트열이라 DEL보다 응답이 빠르다.
    훑는 도중 지우면 구현에 따라 키를 건너뛸 수 있어, 지울 것이 없을 때까지 다시 훑는다.
    """
    r = await get_redis()
    pattern = f"tile:{_glob_escape(dataset_id)}:*"
    removed = 0
    while True:
        removed_this_pass = 0
        batch: list[bytes] = []
        async for key in r.scan_iter(match=pattern, count=PURGE_BATCH_KEYS):
            batch.append(key)
            if len(batch) >= PURGE_BATCH_KEYS:
                removed_this_pass += await r.unlink(*batch)
                batch.clear()
        if batch:
            removed_this_pass += await r.unlink(*batch)
        removed += removed_this_pass
        if removed_this_pass == 0:
            return removed


def _glob_escape(text: str) -> str:
    """Redis MATCH 패턴의 특수 문자(* ? [ ] \\)를 글자 그대로 쓰게 한다."""
    return "".join("\\" + ch if ch in "*?[]\\" else ch for ch in text)


# --- 세션 캐시 ---
#
# session:{id}               세션 상태 JSON (TTL)
# dataset_sessions:{dataset} 그 데이터셋을 보는 세션 ID 집합. 데이터셋을 지울 때 세션 키
#                            전체를 SCAN하지 않으려고 둔다. 세션이 만료돼도 ID가 남을 수
#                            있지만, 지울 때 없는 키를 지우는 것뿐이라 해가 없다.


def session_key(session_id: str) -> str:
    return f"session:{session_id}"


def dataset_sessions_key(dataset_id: str) -> str:
    return f"dataset_sessions:{dataset_id}"


async def get_session(session_id: str) -> dict[str, Any] | None:
    r = await get_redis()
    data = await r.get(session_key(session_id))
    if data is None:
        return None
    return json.loads(data)


async def set_session(session_id: str, state: dict[str, Any], ttl: int | None = None) -> None:
    """세션 상태를 JSON으로 저장한다. JSON이 아닌 값(datetime 등)은 str로 바꾼다.

    데이터셋별 세션 색인에도 올린다. 색인의 TTL은 세션 TTL과 같이 늘려, 그 데이터셋의
    세션이 모두 만료되면 색인도 사라지게 한다.
    """
    r = await get_redis()
    ttl_s = ttl or settings.session_ttl
    async with r.pipeline(transaction=False) as pipe:
        pipe.set(session_key(session_id), json.dumps(state, default=str), ex=ttl_s)
        if state.get("dataset_id"):
            index = dataset_sessions_key(state["dataset_id"])
            pipe.sadd(index, session_id)
            pipe.expire(index, ttl_s)
        await pipe.execute()


async def delete_session(session_id: str, dataset_id: str | None = None) -> None:
    r = await get_redis()
    async with r.pipeline(transaction=False) as pipe:
        pipe.delete(session_key(session_id))
        if dataset_id:
            pipe.srem(dataset_sessions_key(dataset_id), session_id)
        await pipe.execute()


async def refresh_session_ttl(
    session_id: str, ttl: int | None = None, dataset_id: str | None = None
) -> None:
    """세션 TTL을 다시 늘린다. dataset_id를 주면 그 데이터셋의 세션 색인도 함께 늘린다."""
    r = await get_redis()
    ttl_s = ttl or settings.session_ttl
    async with r.pipeline(transaction=False) as pipe:
        pipe.expire(session_key(session_id), ttl_s)
        if dataset_id:
            pipe.expire(dataset_sessions_key(dataset_id), ttl_s)
        await pipe.execute()


async def delete_sessions_for_dataset(dataset_id: str) -> int:
    """주어진 데이터셋을 보는 세션을 모두 지우고 지운 개수를 돌려준다.

    데이터셋별 세션 색인으로 찾으므로 전체 세션 수와 무관하다.
    """
    r = await get_redis()
    index = dataset_sessions_key(dataset_id)
    session_ids = await r.smembers(index)
    deleted = 0
    if session_ids:
        keys = [session_key(raw.decode() if isinstance(raw, bytes) else raw) for raw in session_ids]
        deleted = await r.delete(*keys)
    await r.delete(index)
    return deleted
