"""HTTP·WebSocket 라우트의 API 키 인증.

`settings.api_keys`가 비어 있으면 인증을 끈다 (개발 모드).
"""

import hashlib
import hmac
import logging

from fastapi import Header, HTTPException, status
from starlette.websockets import WebSocket

from webchart.config import settings

logger = logging.getLogger("webchart.auth")


def hash_api_key(key: str | None) -> str | None:
    """키의 SHA-256 해시. 원본 키를 보관하지 않고 저장·비교하기 위해 쓴다."""
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def ws_api_key(websocket: WebSocket) -> str | None:
    """WebSocket 요청에서 키를 꺼낸다. 헤더가 우선이고, 없으면 `api_key` 쿼리 파라미터다."""
    return websocket.headers.get(settings.api_key_header.lower()) or websocket.query_params.get(
        "api_key"
    )


def key_is_valid(candidate: str | None) -> bool:
    """등록된 키 중 하나와 같은지. 타이밍 공격을 막으려고 `hmac.compare_digest`로 비교한다."""
    if not settings.api_keys:
        return True
    if not candidate:
        return False
    return any(hmac.compare_digest(candidate, k) for k in settings.api_keys)


async def require_api_key(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """HTTP 라우트용 FastAPI 의존성: `X-API-Key` 헤더가 유효하지 않으면 401을 낸다."""
    if not key_is_valid(api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Invalid or missing API key"},
            headers={"WWW-Authenticate": f'ApiKey realm="{settings.api_key_header}"'},
        )


async def require_api_key_ws(websocket: WebSocket) -> bool:
    """WebSocket의 API 키를 검사한다. 거부하면 소켓을 4401로 닫고 False를 돌려준다.

    브라우저 WebSocket 클라이언트는 임의 헤더를 붙일 수 없어서 `api_key` 쿼리
    파라미터도 받는다.
    """
    if not settings.api_keys:
        return True
    candidate = ws_api_key(websocket)
    if not key_is_valid(candidate):
        await websocket.close(code=4401, reason="invalid api key")
        return False
    return True
