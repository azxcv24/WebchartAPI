"""WebSocket message dispatch and tile streaming."""

import json
import logging
import struct

from starlette.websockets import WebSocket, WebSocketDisconnect

from webchart.cache.redis_cache import chart_spec_hash
from webchart.models.session import ViewportWindow
from webchart.models.ws_messages import (
    ErrorMessage,
    HoverDataMessage,
    TileInfo,
    TilesMetaMessage,
    ViewportAckMessage,
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

    logger.info(f"WebSocket connected for session {session_id}")

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "")

            if msg_type == "viewport_change":
                await _handle_viewport_change(websocket, session_id, msg)
            elif msg_type == "hover":
                await _handle_hover(websocket, session_id, msg)
            elif msg_type == "scroll":
                await _handle_scroll(websocket, session_id, msg)
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json(
                    ErrorMessage(
                        code="UNKNOWN_MESSAGE", message=f"Unknown type: {msg_type}"
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
    msg: dict,
) -> None:
    """Handle viewport change: compute and stream tiles."""
    viewport = ViewportWindow(
        x_range=tuple(msg["x_range"]),
        y_range=tuple(msg["y_range"]) if msg.get("y_range") else None,
        zoom=msg.get("zoom", 0),
    )

    state = await update_viewport(session_id, viewport)
    spec = state.chart_config
    c_hash = chart_spec_hash(spec.model_dump())

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
    msg: dict,
) -> None:
    """Handle hover event: resolve nearest data point."""
    state = await get_session(session_id)
    result = await resolve_hover(state, msg["pixel_x"], msg["pixel_y"])

    if result:
        await websocket.send_json(result.model_dump())


async def _handle_scroll(
    websocket: WebSocket,
    session_id: str,
    msg: dict,
) -> None:
    """Handle scroll: shift viewport and send new edge tiles."""
    state = await get_session(session_id)
    vp = state.viewport

    delta = msg.get("delta", 100)
    direction = msg.get("direction", "right")

    # Compute viewport shift in data space
    x_range_width = vp.x_range[1] - vp.x_range[0]
    shift_ratio = delta / vp.pixel_width if vp.pixel_width else 0.1
    shift = x_range_width * shift_ratio

    if direction == "right":
        new_x_range = (vp.x_range[0] + shift, vp.x_range[1] + shift)
    elif direction == "left":
        new_x_range = (vp.x_range[0] - shift, vp.x_range[1] - shift)
    else:
        new_x_range = vp.x_range

    # Update and send new tiles
    new_viewport = ViewportWindow(
        x_range=new_x_range,
        y_range=vp.y_range,
        zoom=vp.zoom,
        pixel_width=vp.pixel_width,
        pixel_height=vp.pixel_height,
    )

    await _handle_viewport_change(
        websocket,
        session_id,
        {
            "x_range": list(new_x_range),
            "y_range": list(vp.y_range) if vp.y_range else None,
            "zoom": vp.zoom,
        },
    )
