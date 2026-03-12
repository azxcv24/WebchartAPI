"""Interactive session management API endpoints."""

from fastapi import APIRouter

from webchart.core.errors import WebchartError, webchart_error_to_http
from webchart.models.session import SessionCreate, SessionResponse
from webchart.services.session import manager

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(req: SessionCreate):
    """Create a new interactive chart session."""
    try:
        state = await manager.create_session(
            dataset_id=req.dataset_id,
            chart_config=req.chart_config,
            viewport=req.viewport,
        )
    except WebchartError as e:
        raise webchart_error_to_http(e)

    return SessionResponse(
        session_id=state.session_id,
        dataset_id=state.dataset_id,
        websocket_url=f"/api/v1/ws/{state.session_id}",
        viewport=state.viewport,
        created_at=state.created_at,
    )


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str):
    """Get session state."""
    try:
        state = await manager.get_session(session_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    return SessionResponse(
        session_id=state.session_id,
        dataset_id=state.dataset_id,
        websocket_url=f"/api/v1/ws/{state.session_id}",
        viewport=state.viewport,
        created_at=state.created_at,
    )


@router.delete("/{session_id}", status_code=204)
async def close_session(session_id: str):
    """Close an interactive session."""
    try:
        await manager.close_session(session_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)
