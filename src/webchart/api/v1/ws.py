"""WebSocket endpoint for interactive chart sessions."""

from fastapi import APIRouter
from starlette.websockets import WebSocket

from webchart.services.session.stream import handle_ws_session

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for real-time chart interaction."""
    await handle_ws_session(websocket, session_id)
