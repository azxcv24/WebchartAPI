"""WebSocket 세션 v2: 스트리밍 인터랙션 (계획 문서 4.3, D8 해소).

구조
- 수신 루프는 메시지를 받아 디스패치 테이블(`HANDLERS`)로 넘기기만 한다 (Command 패턴).
- viewport·hover는 작업(Task)으로 돌린다. 같은 종류의 새 요청이 오면 이전 작업을
  취소한다 (latest-wins). 빠르게 패닝해도 요청이 쌓이지 않는다.
- 타일은 다 그릴 때까지 기다리지 않고, 보이는 타일부터 준비되는 대로 보낸다.
  렌더 동시성은 연결마다 세마포어로 묶어, 취소된 프레임의 타일이 렌더 풀에 들어가지 않게 한다.
- 여러 작업이 동시에 보내므로 송신은 잠금으로 한 번에 하나씩 한다.
"""

import asyncio
import json
import logging
import math
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from webchart.api.auth import hash_api_key, ws_api_key
from webchart.application.axes import x_ticks, y_ticks
from webchart.application.interaction import nearest_points, range_summary
from webchart.application.tiles import (
    FramePlan,
    RenderedTile,
    TileRequest,
    default_tile_renderer,
    plan_frame,
)
from webchart.application.windows import lattice_for
from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.errors import WebchartError
from webchart.domain.geometry import MAX_ZOOM, ScreenTransform, TileAddress, XRange
from webchart.domain.ticks import Tick
from webchart.models.dataset import DatasetMeta
from webchart.models.session import SessionState
from webchart.models.ws_messages import (
    PROTOCOL_VERSION,
    TILE_FORMAT_PNG,
    TILE_HEADER_FORMAT,
    TILE_HEADER_MAGIC,
    Crosshair,
    CrosshairValue,
    ErrorMessage,
    FrameBegin,
    FrameEnd,
    HoverRequest,
    PingRequest,
    Pong,
    RangeStats,
    SelectRequest,
    SeriesStats,
    TickMessage,
    TileRef,
    ViewportRequest,
)
from webchart.services.data.access import get_dataset_meta
from webchart.services.session.manager import get_session, update_viewport

logger = logging.getLogger("webchart.session.stream")

# 호버 지점 좌우로 원본을 찾을 폭 (화면 픽셀). 이보다 멀어도 이웃 행으로 찾는다.
HOVER_SEARCH_PX = 8


@dataclass
class Connection:
    """WS 연결 하나의 상태."""

    websocket: WebSocket
    session_id: str
    state: SessionState
    meta: DatasetMeta
    render_slots: asyncio.Semaphore
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    async def send_json(self, message: BaseModel) -> None:
        async with self.send_lock:
            await self.websocket.send_json(message.model_dump(mode="json"))

    async def send_bytes(self, payload: bytes) -> None:
        async with self.send_lock:
            await self.websocket.send_bytes(payload)

    def replace_task(self, kind: str, work: Awaitable[None]) -> None:
        """같은 종류의 진행 중 작업을 취소하고 새 작업을 시작한다 (latest-wins)."""
        previous = self.tasks.get(kind)
        if previous is not None and not previous.done():
            previous.cancel()
        self.tasks[kind] = asyncio.ensure_future(work)

    def cancel_all(self) -> None:
        for task in self.tasks.values():
            task.cancel()


# --- 디스패치 테이블 ----------------------------------------------------------------


async def _on_viewport(connection: Connection, request: BaseModel) -> None:
    assert isinstance(request, ViewportRequest)
    connection.replace_task("frame", _stream_frame(connection, request))


async def _on_hover(connection: Connection, request: BaseModel) -> None:
    assert isinstance(request, HoverRequest)
    connection.replace_task("hover", _answer_hover(connection, request))


async def _on_select(connection: Connection, request: BaseModel) -> None:
    assert isinstance(request, SelectRequest)
    connection.replace_task("select", _answer_select(connection, request))


async def _on_ping(connection: Connection, request: BaseModel) -> None:
    await connection.send_json(Pong())


Handler = Callable[[Connection, BaseModel], Awaitable[None]]

HANDLERS: dict[str, tuple[type[BaseModel], Handler]] = {
    "viewport": (ViewportRequest, _on_viewport),
    "hover": (HoverRequest, _on_hover),
    "select": (SelectRequest, _on_select),
    "ping": (PingRequest, _on_ping),
}


async def handle_ws_session(websocket: WebSocket, session_id: str) -> None:
    """인터랙티브 차트 세션 하나의 WebSocket 처리."""
    await websocket.accept()
    try:
        state = await get_session(session_id)
        meta = await run_in_thread(get_dataset_meta, state.dataset_id)
    except WebchartError as error:
        await websocket.send_json(ErrorMessage(code=error.code, message=error.message).model_dump())
        await websocket.close()
        return

    # 소유자 확인: 세션을 만든 API 키와 같은 키여야 한다 (인증이 꺼져 있었으면 None)
    if state.owner_key_hash is not None:
        if hash_api_key(ws_api_key(websocket)) != state.owner_key_hash:
            await websocket.send_json(
                ErrorMessage(code="FORBIDDEN", message="다른 사용자의 세션이다").model_dump()
            )
            await websocket.close(code=4403)
            return

    connection = Connection(
        websocket, session_id, state, meta, asyncio.Semaphore(settings.render_pool_size)
    )
    logger.info(f"WebSocket 연결: session={session_id}")
    try:
        await _receive_loop(connection)
    except WebSocketDisconnect:
        logger.info(f"WebSocket 끊김: session={session_id}")
    finally:
        connection.cancel_all()


async def _receive_loop(connection: Connection) -> None:
    while True:
        raw = await connection.websocket.receive_text()
        try:
            message = json.loads(raw)
            kind = message.get("type", "") if isinstance(message, dict) else ""
        except json.JSONDecodeError as error:
            await connection.send_json(ErrorMessage(code="INVALID_JSON", message=str(error)))
            continue

        entry = HANDLERS.get(kind)
        if entry is None:
            await connection.send_json(
                ErrorMessage(code="UNKNOWN_MESSAGE", message=f"알 수 없는 type: {kind!r}")
            )
            continue
        model, handler = entry
        try:
            request = model.model_validate(message)
        except ValidationError as error:
            await connection.send_json(
                ErrorMessage(
                    req_id=message.get("req_id")
                    if isinstance(message.get("req_id"), int)
                    else None,
                    code="INVALID_MESSAGE",
                    message=str(error.errors(include_url=False)),
                )
            )
            continue
        await handler(connection, request)


# --- viewport: 프레임 스트리밍 -------------------------------------------------------


async def _stream_frame(connection: Connection, request: ViewportRequest) -> None:
    started = time.perf_counter()
    spec = connection.state.chart_config
    try:
        view = XRange(*request.x_range)
    except ValueError as error:
        await connection.send_json(
            ErrorMessage(req_id=request.req_id, code="INVALID_VIEWPORT", message=str(error))
        )
        return

    try:
        zoom = request.zoom if request.zoom is not None else _auto_zoom(connection, view, request)
        plan = await run_in_thread(plan_frame, connection.meta, spec, view, zoom, request.dpr)
        await _remember_viewport(connection, request, plan, zoom)
        await connection.send_json(_frame_begin(connection, request, plan, view, zoom))

        sent = cached = 0
        exact = True
        for batch in (plan.visible, plan.prefetch):  # 보이는 타일 먼저
            pending = [asyncio.ensure_future(_render(connection, plan, tile)) for tile in batch]
            try:
                for finished in asyncio.as_completed(pending):
                    tile = await finished
                    await connection.send_bytes(_tile_frame(request.req_id, tile))
                    sent += 1
                    cached += tile.source == "cache"
                    exact &= tile.is_exact
            finally:
                for task in pending:
                    task.cancel()

        elapsed_ms = (time.perf_counter() - started) * 1000
        await connection.send_json(FrameEnd(req_id=request.req_id, sent=sent, cached=cached,
                                            exact=exact, elapsed_ms=elapsed_ms))  # fmt: skip
    except asyncio.CancelledError:
        raise  # 새 viewport가 이 프레임을 대체했다
    except Exception as error:
        logger.exception("프레임 렌더 실패")
        await connection.send_json(
            ErrorMessage(req_id=request.req_id, code="RENDER_ERROR", message=str(error))
        )


async def _render(connection: Connection, plan: FramePlan, tile: TileAddress) -> RenderedTile:
    # 세마포어 대기 중에 취소되면 렌더 풀에 일을 넣지 않는다
    async with connection.render_slots:
        request = TileRequest(
            connection.meta, connection.state.chart_config, plan.lattice, tile, plan.dpr,
            plan.y_ranges,
        )  # fmt: skip
        return await default_tile_renderer.render(request)


def _auto_zoom(connection: Connection, view: XRange, request: ViewportRequest) -> int:
    """타일 한 픽셀이 화면 한 픽셀 이하가 되는 가장 얕은 줌."""
    lattice = lattice_for(connection.meta, connection.state.chart_config.x_axis.column,
                          settings.tile_size)  # fmt: skip
    ratio = request.width_px * lattice.domain.width / (view.width * settings.tile_size)
    return min(MAX_ZOOM, max(0, math.ceil(math.log2(max(ratio, 1e-12)) - 1e-9)))


def _tile_frame(req_id: int, tile: RenderedTile) -> bytes:
    header = struct.pack(
        TILE_HEADER_FORMAT,
        TILE_HEADER_MAGIC,
        PROTOCOL_VERSION,
        req_id,
        tile.tile.zoom,
        tile.tile.index,
        tile.dpr,
        TILE_FORMAT_PNG,
    )
    return header + tile.image


def _frame_begin(
    connection: Connection, request: ViewportRequest, plan: FramePlan, view: XRange, zoom: int
) -> FrameBegin:
    spec = connection.state.chart_config
    x_info = next((c for c in connection.meta.columns if c.name == spec.x_axis.column), None)
    tiles = [
        TileRef(zoom=t.zoom, index=t.index, visible=is_visible,
                x_start=plan.lattice.tile_range(t).start, x_end=plan.lattice.tile_range(t).end)
        for batch, is_visible in ((plan.visible, True), (plan.prefetch, False))
        for t in batch
    ]  # fmt: skip
    return FrameBegin(
        req_id=request.req_id,
        zoom=zoom,
        dpr=plan.dpr,
        tile_size_px=plan.lattice.tile_size_px * plan.dpr,
        x_range=view.as_tuple(),
        tiles=tiles,
        y_ranges={name: r.as_tuple() for name, r in plan.y_ranges.items()},
        x_ticks=_tick_messages(x_ticks(spec, x_info, view, request.width_px)),
        y_ticks={
            name: _tick_messages(y_ticks(r, request.height_px), flip_height=request.height_px)
            for name, r in plan.y_ranges.items()
        },
    )


def _tick_messages(ticks: list[Tick], flip_height: float | None = None) -> list[TickMessage]:
    """눈금 → 메시지. y축은 화면 좌표(위쪽이 0)로 뒤집는다."""
    return [
        TickMessage(
            value=t.value,
            label=t.label,
            pixel=t.pixel if flip_height is None else flip_height - t.pixel,
        )
        for t in ticks
    ]


async def _remember_viewport(
    connection: Connection, request: ViewportRequest, plan: FramePlan, zoom: int
) -> None:
    """서버가 확정한 화면 상태를 세션에 남긴다 (호버 좌표 변환, 재접속용)."""
    primary = plan.y_ranges.get("y") or plan.y_ranges.get("y2")
    viewport = connection.state.viewport.model_copy(
        update={
            "x_range": request.x_range,
            "y_range": primary.as_tuple() if primary else None,
            "zoom": zoom,
            "dpr": request.dpr,
            "pixel_width": request.width_px,
            "pixel_height": request.height_px,
        }
    )
    connection.state = await update_viewport(connection.session_id, viewport)


# --- hover / select ----------------------------------------------------------------


async def _answer_hover(connection: Connection, request: HoverRequest) -> None:
    viewport = connection.state.viewport
    y_low, y_high = viewport.y_range or (0.0, 1.0)
    try:
        transform = ScreenTransform(
            XRange(*viewport.x_range), y_low, y_high, viewport.pixel_width, viewport.pixel_height
        )
    except ValueError as error:
        await connection.send_json(
            ErrorMessage(req_id=request.req_id, code="NO_VIEWPORT", message=str(error))
        )
        return
    hover_x, _ = transform.to_data(request.pixel_x, request.pixel_y)
    spec = connection.state.chart_config
    points = await run_in_thread(
        nearest_points,
        connection.meta,
        spec.x_axis.column,
        spec.data_columns,
        hover_x,
        transform.x_per_px * HOVER_SEARCH_PX,
        spec.x_axis.timezone,
    )
    await connection.send_json(
        Crosshair(
            req_id=request.req_id,
            x=hover_x,
            points=[CrosshairValue(series=p.series, x=p.x, y=p.y, label=p.label) for p in points],
        )
    )


async def _answer_select(connection: Connection, request: SelectRequest) -> None:
    try:
        x_range = XRange(*request.x_range)
    except ValueError as error:
        await connection.send_json(
            ErrorMessage(req_id=request.req_id, code="INVALID_RANGE", message=str(error))
        )
        return
    spec = connection.state.chart_config
    summary = await run_in_thread(
        range_summary, connection.meta, spec.x_axis.column, spec.data_columns, x_range
    )
    await connection.send_json(
        RangeStats(
            req_id=request.req_id,
            x_range=request.x_range,
            exact=summary.is_exact,
            source=summary.source,
            series={name: SeriesStats(**_as_dict(s)) for name, s in summary.series.items()},
        )
    )


def _as_dict(summary: Any) -> dict[str, Any]:
    return {name: getattr(summary, name) for name in summary.__dataclass_fields__}
