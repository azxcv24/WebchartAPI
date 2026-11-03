"""인터랙티브 차트 세션의 WebSocket 엔드포인트."""

from fastapi import APIRouter
from starlette.websockets import WebSocket

from webchart.api.auth import require_api_key_ws
from webchart.services.session.stream import handle_ws_session

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """세션의 실시간 차트 인터랙션 채널. 프로토콜은 services/session/stream.py가 처리한다."""
    if not await require_api_key_ws(websocket):
        return
    await handle_ws_session(websocket, session_id)
