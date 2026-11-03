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


async def get_tile(key: str) -> bytes | None:
    r = await get_redis()
    return await r.get(key)


async def set_tile(key: str, data: bytes, ttl: int | None = None) -> None:
    """타일을 저장한다. ttl(초)을 주지 않으면 설정의 tile_cache_ttl을 쓴다."""
    r = await get_redis()
    await r.set(key, data, ex=ttl or settings.tile_cache_ttl)


# --- 세션 캐시 ---


def session_key(session_id: str) -> str:
    return f"session:{session_id}"


async def get_session(session_id: str) -> dict[str, Any] | None:
    r = await get_redis()
    data = await r.get(session_key(session_id))
    if data is None:
        return None
    return json.loads(data)


async def set_session(session_id: str, state: dict[str, Any], ttl: int | None = None) -> None:
    """세션 상태를 JSON으로 저장한다. JSON이 아닌 값(datetime 등)은 str로 바꾼다."""
    r = await get_redis()
    await r.set(
        session_key(session_id),
        json.dumps(state, default=str),
        ex=ttl or settings.session_ttl,
    )


async def delete_session(session_id: str) -> None:
    r = await get_redis()
    await r.delete(session_key(session_id))


async def refresh_session_ttl(session_id: str, ttl: int | None = None) -> None:
    r = await get_redis()
    await r.expire(session_key(session_id), ttl or settings.session_ttl)


async def delete_sessions_for_dataset(dataset_id: str) -> int:
    """주어진 데이터셋을 참조하는 세션을 모두 지우고 지운 개수를 돌려준다.

    세션 키 전체를 SCAN하므로 세션 수에 비례한다. 데이터셋 삭제 때만 쓴다.
    """
    r = await get_redis()
    deleted = 0
    async for raw_key in r.scan_iter(match="session:*"):
        data = await r.get(raw_key)
        if data is None:
            continue
        try:
            state = json.loads(data)
        except json.JSONDecodeError:
            continue
        if state.get("dataset_id") == dataset_id:
            await r.delete(raw_key)
            deleted += 1
    return deleted
