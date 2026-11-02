"""Redis cache layer for tiles, queries, and sessions."""

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
    """Eagerly create the Redis pool (called from FastAPI lifespan)."""
    global _pool
    async with _init_lock:
        if _pool is None:
            _pool = redis.from_url(
                settings.redis_url,
                decode_responses=False,  # binary for tile images
            )
    return _pool


async def get_redis() -> redis.Redis:
    """Get the Redis pool, initializing under a lock if needed."""
    if _pool is not None:
        return _pool
    return await init_redis()


async def close_redis() -> None:
    """Close Redis connection pool."""
    global _pool
    async with _init_lock:
        if _pool is not None:
            # aclose() is the non-deprecated async close in redis-py >= 5.0.1.
            closer = getattr(_pool, "aclose", None)
            if closer is not None:
                await closer()
            else:  # pragma: no cover - older redis-py fallback
                await _pool.close()
            _pool = None


# --- Tile Cache ---

# 키 형식은 application/tiles.py의 TileRequest.cache_key가 정한다.


async def get_tile(key: str) -> bytes | None:
    r = await get_redis()
    return await r.get(key)


async def set_tile(key: str, data: bytes, ttl: int | None = None) -> None:
    r = await get_redis()
    await r.set(key, data, ex=ttl or settings.tile_cache_ttl)


# --- Session Cache ---

def session_key(session_id: str) -> str:
    return f"session:{session_id}"


async def get_session(session_id: str) -> dict[str, Any] | None:
    r = await get_redis()
    data = await r.get(session_key(session_id))
    if data is None:
        return None
    return json.loads(data)


async def set_session(session_id: str, state: dict[str, Any], ttl: int | None = None) -> None:
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
    """Delete every session that references a given dataset. Returns count."""
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
