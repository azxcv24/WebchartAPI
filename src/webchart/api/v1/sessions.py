"""인터랙티브 세션 관리 API.

세션은 만든 호출자의 API 키 해시를 기록하고, 조회·종료는 같은 키로만 허용한다.
"""

from fastapi import APIRouter, Header, HTTPException, status

from webchart.api.auth import hash_api_key
from webchart.core.errors import WebchartError, webchart_error_to_http
from webchart.models.session import SessionCreate, SessionResponse
from webchart.services.session import manager

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _check_owner(state, x_api_key: str | None) -> None:
    """세션 소유 키와 다른 API 키로 접근하면 403을 낸다."""
    expected = state.owner_key_hash
    if expected is None:
        return  # 소유자 기록이 없는 이전 세션이거나, 생성 시 키 없이 만든 세션
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
    """인터랙티브 차트 세션을 만든다. 응답의 websocket_url로 연결한다."""
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
    """세션 상태를 조회한다."""
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
    """세션을 닫는다."""
    try:
        state = await manager.get_session(session_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)
    _check_owner(state, x_api_key)
    try:
        await manager.close_session(session_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)
