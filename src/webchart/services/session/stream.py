"""WebSocket message dispatch and tile streaming."""

import json
import logging
import struct

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from webchart.api.auth import hash_api_key, ws_api_key
from webchart.cache.redis_cache import tile_spec_hash
from webchart.models.session import ViewportWindow
from webchart.models.ws_messages import (
    ErrorMessage,
    HoverMessage,
    ScrollMessage,
    TileInfo,
    TilesMetaMessage,
    ViewportAckMessage,
    ViewportChangeMessage,
)
from webchart.services.render.tile_renderer import render_visible_tiles
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
    """Handle viewport change: compute and stream tiles."""
    # Preserve existing viewport pixel dimensions if the message omits them
    # (e.g. when called from the scroll handler) so hover→pixel math stays correct.
    existing = (await get_session(session_id)).viewport
    viewport = ViewportWindow(
        x_range=msg.x_range,
        y_range=msg.y_range if msg.y_range is not None else existing.y_range,
        zoom=msg.zoom,
        pixel_width=existing.pixel_width,
        pixel_height=existing.pixel_height,
    )

    state = await update_viewport(session_id, viewport)
    spec = state.chart_config
    c_hash = tile_spec_hash(spec.model_dump())

    # Render tiles
    tiles = await render_visible_tiles(
        state.dataset_id,
        spec,
        viewport.x_range,
        viewport.zoom,
        c_hash,
    )

    # Send tiles metadata
    tile_infos = [
        TileInfo(
            tile_id=tid,
            x=int(tid.split("-")[1]),
            y=int(tid.split("-")[2]),
            width=512,
            height=512,
            ready=True,
        )
        for tid in tiles
    ]
    await websocket.send_json(
        TilesMetaMessage(tiles=tile_infos).model_dump()
    )

    # Stream tile images as binary frames
    for tile_id, tile_bytes in tiles.items():
        tile_id_bytes = tile_id.encode("utf-8")
        header = struct.pack(">I", len(tile_id_bytes))
        await websocket.send_bytes(header + tile_id_bytes + tile_bytes)

    # Ack
    await websocket.send_json(
        ViewportAckMessage(
            x_range=viewport.x_range,
            zoom=viewport.zoom,
            tile_count=len(tiles),
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
        ViewportChangeMessage(
            x_range=new_x_range,
            y_range=vp.y_range,
            zoom=vp.zoom,
        ),
    )
