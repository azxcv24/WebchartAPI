"""Interactive session management API endpoints."""

from fastapi import APIRouter, Header, HTTPException, status

from webchart.api.auth import hash_api_key
from webchart.core.errors import WebchartError, webchart_error_to_http
from webchart.models.session import SessionCreate, SessionResponse
from webchart.services.session import manager

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _check_owner(state, x_api_key: str | None) -> None:
    """Reject access if the session is owned by a different API key."""
    expected = state.owner_key_hash
    if expected is None:
        return  # legacy session or auth disabled at creation time
    if hash_api_key(x_api_key) != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "Session belongs to another caller"},
        )


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    req: SessionCreate,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Create a new interactive chart session."""
    try:
        state = await manager.create_session(
            dataset_id=req.dataset_id,
            chart_config=req.chart_config,
            viewport=req.viewport,
            owner_key_hash=hash_api_key(x_api_key),
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
async def get_session(
    session_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Get session state."""
    try:
        state = await manager.get_session(session_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    _check_owner(state, x_api_key)

    return SessionResponse(
        session_id=state.session_id,
        dataset_id=state.dataset_id,
        websocket_url=f"/api/v1/ws/{state.session_id}",
        viewport=state.viewport,
        created_at=state.created_at,
    )


@router.delete("/{session_id}", status_code=204)
async def close_session(
    session_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Close an interactive session."""
    try:
        state = await manager.get_session(session_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)
    _check_owner(state, x_api_key)
    try:
        await manager.close_session(session_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)
