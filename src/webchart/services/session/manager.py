"""Session lifecycle management."""

import logging
import uuid
from datetime import datetime, timezone

from webchart.cache import redis_cache
from webchart.core.errors import SessionNotFoundError
from webchart.models.chart import ChartSpec
from webchart.models.session import SessionState, ViewportWindow

logger = logging.getLogger("webchart.session.manager")


async def create_session(
    dataset_id: str,
    chart_config: ChartSpec,
    viewport: ViewportWindow,
) -> SessionState:
    """Create a new interactive chart session."""
    session_id = str(uuid.uuid4())

    state = SessionState(
        session_id=session_id,
        dataset_id=dataset_id,
        chart_config=chart_config,
        viewport=viewport,
    )

    await redis_cache.set_session(session_id, state.model_dump())
    logger.info(f"Created session {session_id} for dataset {dataset_id}")

    return state


async def get_session(session_id: str) -> SessionState:
    """Retrieve session state from cache."""
    data = await redis_cache.get_session(session_id)
    if data is None:
        raise SessionNotFoundError(session_id)

    await redis_cache.refresh_session_ttl(session_id)
    return SessionState.model_validate(data)


async def update_viewport(session_id: str, viewport: ViewportWindow) -> SessionState:
    """Update session viewport and refresh TTL."""
    state = await get_session(session_id)
    state.viewport = viewport
    state.last_active = datetime.now(timezone.utc)

    await redis_cache.set_session(session_id, state.model_dump())
    return state


async def close_session(session_id: str) -> None:
    """Close and clean up a session."""
    await redis_cache.delete_session(session_id)
    logger.info(f"Closed session {session_id}")
