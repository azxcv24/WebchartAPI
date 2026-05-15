"""Redis cache layer for tiles, queries, and sessions."""

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as redis

from webchart.config import settings

logger = logging.getLogger("webchart.cache")

_pool: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    """Get or create Redis connection pool."""
    global _pool
    if _pool is None:
        _pool = redis.from_url(
            settings.redis_url,
            decode_responses=False,  # binary for tile images
        )
    return _pool


async def close_redis() -> None:
    """Close Redis connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# --- Tile Cache ---

def tile_key(dataset_id: str, chart_hash: str, zoom: int, tile_x: int, tile_y: int = 0) -> str:
    return f"tile:{dataset_id}:{chart_hash}:z{zoom}:x{tile_x}:y{tile_y}"


async def get_tile(key: str) -> bytes | None:
    r = await get_redis()
    return await r.get(key)


async def set_tile(key: str, data: bytes, ttl: int | None = None) -> None:
    r = await get_redis()
    await r.set(key, data, ex=ttl or settings.tile_cache_ttl)


async def get_tiles_multi(keys: list[str]) -> dict[str, bytes | None]:
    r = await get_redis()
    values = await r.mget(keys)
    return dict(zip(keys, values))


# --- Query Cache ---

def query_key(dataset_id: str, x_range: tuple[float, float], algo: str, target_n: int) -> str:
    range_hash = hashlib.md5(f"{x_range[0]}:{x_range[1]}".encode()).hexdigest()[:8]
    return f"query:{dataset_id}:{range_hash}:{algo}:{target_n}"


async def get_query_cache(key: str) -> bytes | None:
    r = await get_redis()
    return await r.get(key)


async def set_query_cache(key: str, data: bytes, ttl: int | None = None) -> None:
    r = await get_redis()
    await r.set(key, data, ex=ttl or settings.query_cache_ttl)


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


# --- Utility ---

def chart_spec_hash(spec: dict[str, Any]) -> str:
    """Generate a deterministic hash for a chart specification (full)."""
    canonical = json.dumps(spec, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# Fields that actually affect tile pixels. Title, width/height, format,
# legend etc. do NOT change tile contents (tiles are headless), so excluding
# them lets the same tile cache serve many chart sizes.
_TILE_HASH_FIELDS = (
    "chart_type",
    "x_axis",
    "y_axis",
    "y_columns",
    "series",
    "downsample_algorithm",
    "background_color",
)


def tile_spec_hash(spec: dict[str, Any]) -> str:
    """Deterministic hash for the tile-affecting subset of a chart spec."""
    subset = {k: spec.get(k) for k in _TILE_HASH_FIELDS}
    canonical = json.dumps(subset, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
