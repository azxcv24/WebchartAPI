"""인터랙티브 차트 세션의 생성·조회·갱신·종료. 상태는 Redis에 TTL과 함께 둔다."""

import logging
import uuid
from datetime import UTC, datetime

from webchart.cache import redis_cache
from webchart.core.errors import SessionNotFoundError
from webchart.models.chart import ChartSpec
from webchart.models.session import SessionState, ViewportWindow

logger = logging.getLogger("webchart.session.manager")


async def create_session(
    dataset_id: str,
    chart_config: ChartSpec,
    viewport: ViewportWindow,
    owner_key_hash: str | None = None,
) -> SessionState:
    """새 세션을 만들어 Redis에 저장한다."""
    session_id = str(uuid.uuid4())

    state = SessionState(
        session_id=session_id,
        dataset_id=dataset_id,
        chart_config=chart_config,
        viewport=viewport,
        owner_key_hash=owner_key_hash,
    )

    await redis_cache.set_session(session_id, state.model_dump())
    logger.info(f"Created session {session_id} for dataset {dataset_id}")

    return state


async def get_session(session_id: str) -> SessionState:
    """세션 상태를 읽는다. 읽을 때마다 TTL을 다시 늘린다."""
    data = await redis_cache.get_session(session_id)
    if data is None:
        raise SessionNotFoundError(session_id)

    await redis_cache.refresh_session_ttl(session_id)
    return SessionState.model_validate(data)


async def update_viewport(session_id: str, viewport: ViewportWindow) -> SessionState:
    """화면 상태를 바꾸고 마지막 활동 시각을 갱신한다."""
    state = await get_session(session_id)
    state.viewport = viewport
    state.last_active = datetime.now(UTC)

    await redis_cache.set_session(session_id, state.model_dump())
    return state


async def close_session(session_id: str) -> None:
    """세션 상태를 지운다."""
    await redis_cache.delete_session(session_id)
    logger.info(f"Closed session {session_id}")
