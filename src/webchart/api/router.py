"""Top-level router aggregation."""

from fastapi import APIRouter, Depends

from webchart.api.auth import require_api_key
from webchart.api.v1 import charts, datasets, sessions, ws

# HTTP routes share the api-key dependency. The WebSocket route uses its
# own check (require_api_key_ws) since header auth is awkward in browsers.
api_router = APIRouter(prefix="/api/v1")
http_router = APIRouter(dependencies=[Depends(require_api_key)])

http_router.include_router(datasets.router)
http_router.include_router(charts.router)
http_router.include_router(sessions.router)

api_router.include_router(http_router)
api_router.include_router(ws.router)
# 임베드 이미지·XYZ 타일: 헤더 키 또는 차트별 토큰으로 인증한다 (<img>·CDN은 헤더를 못 붙인다)
api_router.include_router(charts.embed_router)
