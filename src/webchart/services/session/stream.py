"""WebSocket message dispatch and tile streaming."""

import json
import logging
import struct

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from webchart.api.auth import hash_api_key, ws_api_key
from webchart.application.tiles import plan_frame, render_frame
from webchart.core.concurrency import run_in_thread
from webchart.domain.geometry import XRange
from webchart.models.ws_messages import (
    ErrorMessage,
    HoverMessage,
    ScrollMessage,
    TileInfo,
    TilesMetaMessage,
    ViewportAckMessage,
    ViewportChangeMessage,
)
from webchart.services.data.access import get_dataset_meta
from webchart.services.session.interaction import resolve_hover
from webchart.services.session.manager import get_session, update_viewport

logger = logging.getLogger("webchart.session.stream")


async def handle_ws_session(websocket: WebSocket, session_id: str) -> None:
    """Main WebSocket handler for an interactive chart session."""
    await websocket.accept()

    try:
        state = await get_session(session_id)
    except Exception as e:
        await websocket.send_json(
            ErrorMessage(code="SESSION_NOT_FOUND", message=str(e)).model_dump()
        )
        await websocket.close()
        return

    # Owner check: if the session recorded a key hash at creation, the
    # caller must present the same key. (None hash means auth was off.)
    if state.owner_key_hash is not None:
        if hash_api_key(ws_api_key(websocket)) != state.owner_key_hash:
            await websocket.send_json(
                ErrorMessage(
                    code="FORBIDDEN",
                    message="Session belongs to another caller",
                ).model_dump()
            )
            await websocket.close(code=4403)
            return

    logger.info(f"WebSocket connected for session {session_id}")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                await websocket.send_json(
                    ErrorMessage(code="INVALID_JSON", message=str(e)).model_dump()
                )
                continue

            msg_type = msg.get("type", "")
            try:
                if msg_type == "viewport_change":
                    parsed = ViewportChangeMessage.model_validate(msg)
                    await _handle_viewport_change(websocket, session_id, parsed)
                elif msg_type == "hover":
                    parsed_hover = HoverMessage.model_validate(msg)
                    await _handle_hover(websocket, session_id, parsed_hover)
                elif msg_type == "scroll":
                    parsed_scroll = ScrollMessage.model_validate(msg)
                    await _handle_scroll(websocket, session_id, parsed_scroll)
                elif msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    await websocket.send_json(
                        ErrorMessage(
                            code="UNKNOWN_MESSAGE",
                            message=f"Unknown type: {msg_type}",
                        ).model_dump()
                    )
            except ValidationError as e:
                await websocket.send_json(
                    ErrorMessage(
                        code="INVALID_MESSAGE", message=e.errors(include_url=False).__str__()
                    ).model_dump()
                )

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for session {session_id}")
    except Exception as e:
        logger.error(f"WebSocket error for session {session_id}: {e}")
        try:
            await websocket.send_json(
                ErrorMessage(code="INTERNAL_ERROR", message=str(e)).model_dump()
            )
        except Exception:
            pass


async def _handle_viewport_change(
    websocket: WebSocket,
    session_id: str,
    msg: ViewportChangeMessage,
) -> None:
    """뷰포트 변경: 프레임을 계획(타일·공유 y범위)하고 타일을 그려 보낸다."""
    state = await get_session(session_id)
    existing = state.viewport
    spec = state.chart_config
    meta = await run_in_thread(get_dataset_meta, state.dataset_id)
    dpr = msg.dpr or existing.dpr

    try:
        view = XRange(*msg.x_range)
    except ValueError as error:
        await websocket.send_json(
            ErrorMessage(code="INVALID_VIEWPORT", message=str(error)).model_dump()
        )
        return
    plan = await run_in_thread(plan_frame, meta, spec, view, msg.zoom, dpr)

    # 서버가 확정한 y범위를 세션에 남겨 호버 좌표 변환이 타일과 같은 스케일을 쓰게 한다
    viewport = existing.model_copy(
        update={
            "x_range": msg.x_range,
            "y_range": plan.y_range.as_tuple(),
            "zoom": msg.zoom,
            "dpr": dpr,
        }
    )
    await update_viewport(session_id, viewport)

    rendered = await render_frame(meta, spec, plan)
    tile_size_px = plan.lattice.tile_size_px * dpr
    await websocket.send_json(
        TilesMetaMessage(
            tiles=[
                TileInfo(
                    tile_id=tile.tile.tile_id,
                    x=tile.tile.index,
                    y=0,
                    width=tile_size_px,
                    height=tile_size_px,
                    ready=True,
                )
                for tile in rendered
            ]
        ).model_dump()
    )
    for tile in rendered:
        tile_id_bytes = tile.tile.tile_id.encode("utf-8")
        header = struct.pack(">I", len(tile_id_bytes))
        await websocket.send_bytes(header + tile_id_bytes + tile.image)

    await websocket.send_json(
        ViewportAckMessage(
            x_range=viewport.x_range,
            zoom=viewport.zoom,
            tile_count=len(rendered),
            y_range=plan.y_range.as_tuple(),
            exact=all(tile.is_exact for tile in rendered),
        ).model_dump()
    )


async def _handle_hover(
    websocket: WebSocket,
    session_id: str,
    msg: HoverMessage,
) -> None:
    """Handle hover event: resolve nearest data point."""
    state = await get_session(session_id)
    result = await resolve_hover(state, msg.pixel_x, msg.pixel_y)

    if result:
        await websocket.send_json(result.model_dump())


async def _handle_scroll(
    websocket: WebSocket,
    session_id: str,
    msg: ScrollMessage,
) -> None:
    """Handle scroll: shift viewport and send new edge tiles."""
    state = await get_session(session_id)
    vp = state.viewport

    # Compute viewport shift in data space
    x_range_width = vp.x_range[1] - vp.x_range[0]
    shift_ratio = msg.delta / vp.pixel_width if vp.pixel_width else 0.1
    shift = x_range_width * shift_ratio

    if msg.direction == "right":
        new_x_range = (vp.x_range[0] + shift, vp.x_range[1] + shift)
    elif msg.direction == "left":
        new_x_range = (vp.x_range[0] - shift, vp.x_range[1] - shift)
    else:
        new_x_range = vp.x_range

    await _handle_viewport_change(
        websocket,
        session_id,
        ViewportChangeMessage(x_range=new_x_range, zoom=vp.zoom, dpr=vp.dpr),
    )
