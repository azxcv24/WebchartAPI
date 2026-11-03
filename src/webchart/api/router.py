"""`/api/v1` 아래 라우터를 하나로 묶는다."""

from fastapi import APIRouter, Depends

from webchart.api.auth import require_api_key
from webchart.api.v1 import charts, datasets, sessions, ws

# HTTP 라우트는 API 키 의존성을 공유한다. 브라우저 WebSocket은 헤더를 붙이기 어려워서
# WebSocket 라우트는 자체 검사(require_api_key_ws)를 쓴다.
api_router = APIRouter(prefix="/api/v1")
http_router = APIRouter(dependencies=[Depends(require_api_key)])

http_router.include_router(datasets.router)
http_router.include_router(charts.router)
http_router.include_router(sessions.router)

api_router.include_router(http_router)
api_router.include_router(ws.router)
# 임베드 이미지·XYZ 타일: 헤더 키 또는 차트별 토큰으로 인증한다 (<img>·CDN은 헤더를 못 붙인다)
api_router.include_router(charts.embed_router)
