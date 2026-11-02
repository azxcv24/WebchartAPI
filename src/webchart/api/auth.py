"""API key authentication for HTTP and WebSocket routes."""

import hashlib
import hmac
import logging

from fastapi import Header, HTTPException, status
from starlette.websockets import WebSocket

from webchart.config import settings

logger = logging.getLogger("webchart.auth")


def hash_api_key(key: str | None) -> str | None:
    """Return a stable hash so we can store/compare keys without retaining them."""
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def ws_api_key(websocket: WebSocket) -> str | None:
    return (
        websocket.headers.get(settings.api_key_header.lower())
        or websocket.query_params.get("api_key")
    )


def key_is_valid(candidate: str | None) -> bool:
    if not settings.api_keys:
        # No keys configured -> auth disabled (dev mode).
        return True
    if not candidate:
        return False
    return any(hmac.compare_digest(candidate, k) for k in settings.api_keys)


async def require_api_key(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency: enforce X-API-Key header on HTTP routes."""
    if not key_is_valid(api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Invalid or missing API key"},
            headers={"WWW-Authenticate": f'ApiKey realm="{settings.api_key_header}"'},
        )


async def require_api_key_ws(websocket: WebSocket) -> bool:
    """Validate API key on a WebSocket. Closes the socket and returns False
    on rejection so callers can short-circuit.

    Accepts the key from either the X-API-Key header or an `api_key` query
    parameter (browser WebSocket clients can't set custom headers).
    """
    if not settings.api_keys:
        return True
    candidate = ws_api_key(websocket)
    if not key_is_valid(candidate):
        await websocket.close(code=4401, reason="invalid api key")
        return False
    return True
