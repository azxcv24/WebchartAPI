"""타일 렌더 유스케이스 (계획 문서 1.5, 3.2).

파이프라인: plan(격자·레벨·y범위) → fetch(피라미드|원본) → project(M4) → paint → encode(PNG).
`CachedTileRenderer`(데코레이터)가 파이프라인을 감싸 캐시를 조회·저장한다.

한 화면(프레임)의 모든 타일은 같은 y범위를 쓴다(D1). y범위는 캐시 키에 들어가므로
다른 y범위로 그린 타일이 섞이지 않는다.
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Protocol

from webchart.application.windows import fetch_tile_windows, lattice_for
from webchart.application.y_ranges import resolve_frame_y_range
from webchart.cache import redis_cache
from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.domain.geometry import PixelLattice, TileAddress, XRange
from webchart.domain.projection import m4_polyline
from webchart.domain.y_range import YRange
from webchart.models.chart import ChartSpec, ChartType, DownsampleAlgorithm
from webchart.models.dataset import DatasetMeta
from webchart.services.render.matplotlib_renderer import (
    MatplotlibRenderer,
    series_lines,
    spec_y_columns,
)

logger = logging.getLogger("webchart.application.tiles")

# 프리페치: 보이는 타일 좌우로 미리 그려 둘 타일 수
PREFETCH_RADIUS = 2

_painter = MatplotlibRenderer()


@dataclass(frozen=True, slots=True)
class TileRequest:
    """타일 하나를 그리는 데 필요한 모든 것. 같은 요청은 같은 그림이다 (캐시 키의 근거)."""

    meta: DatasetMeta
    spec: ChartSpec
    lattice: PixelLattice
    tile: TileAddress
    dpr: int
    y_range: YRange

    @property
    def size_px(self) -> int:
        return self.lattice.tile_size_px * self.dpr

    def cache_key(self) -> str:
        """`tile:{dataset}:{data_version}:{그림 해시}:{z}-{i}@{dpr}x`"""
        return (
            f"tile:{self.meta.dataset_id}:{self.meta.data_version or 'v0'}:"
            f"{render_hash(self.spec, self.y_range, self.lattice.tile_size_px)}:"
            f"{self.tile.zoom}-{self.tile.index}@{self.dpr}x"
        )


@dataclass(frozen=True, slots=True)
class RenderedTile:
    tile: TileAddress
    dpr: int
    image: bytes
    is_exact: bool
    source: str  # "raw", "pyramid:L{n}", "cache"


class TileRenderer(Protocol):
    async def render(self, request: TileRequest) -> RenderedTile: ...


class PipelineTileRenderer:
    """캐시 없이 타일을 실제로 그린다."""

    async def render(self, request: TileRequest) -> RenderedTile:
        return await run_in_thread(self._render_sync, request)

    @staticmethod
    def _render_sync(request: TileRequest) -> RenderedTile:
        spec = request.spec
        x_column = spec.x_axis.column
        y_columns = spec_y_columns(spec)
        windows = fetch_tile_windows(
            request.meta, x_column, y_columns, request.lattice, request.tile, request.dpr
        )
        polylines = {series: m4_polyline(window) for series, window in windows.windows.items()}
        grid = request.lattice.tile_grid(request.tile, request.dpr)
        image = _painter.render_raster(
            series_lines(spec, polylines),
            spec.chart_type,
            grid.x_range,
            request.y_range,
            width_px=request.size_px,
            height_px=request.size_px,
            dpr=request.dpr,
            background_color=spec.background_color,
        )
        return RenderedTile(
            tile=request.tile,
            dpr=request.dpr,
            image=image,
            is_exact=windows.is_exact and is_exact_spec(spec),
            source=windows.source,
        )


class CachedTileRenderer:
    """데코레이터: 캐시에 있으면 그대로, 없으면 내부 렌더러로 그려 저장한다.

    캐시 장애는 렌더를 막지 않는다 (경고만 남기고 캐시 없이 그린다).
    """

    def __init__(self, inner: TileRenderer) -> None:
        self._inner = inner

    async def render(self, request: TileRequest) -> RenderedTile:
        key = request.cache_key()
        try:
            cached = await redis_cache.get_tile(key)
        except Exception as error:
            logger.debug(f"타일 캐시 조회 실패, 캐시 없이 그린다: {error}")
            cached = None
        if cached is not None:
            return RenderedTile(
                request.tile, request.dpr, cached, is_exact_spec(request.spec), "cache"
            )

        rendered = await self._inner.render(request)
        try:
            await redis_cache.set_tile(key, rendered.image)
        except Exception as error:
            logger.debug(f"타일 캐시 저장 실패: {error}")
        return rendered


default_tile_renderer: TileRenderer = CachedTileRenderer(PipelineTileRenderer())


@dataclass(frozen=True, slots=True)
class FramePlan:
    """뷰포트 하나를 채우는 타일 목록과 공유 y범위."""

    lattice: PixelLattice
    visible: list[TileAddress]
    prefetch: list[TileAddress]
    y_range: YRange
    dpr: int


def plan_frame(
    meta: DatasetMeta,
    spec: ChartSpec,
    view: XRange,
    zoom: int,
    dpr: int = 1,
    prefetch_radius: int = PREFETCH_RADIUS,
) -> FramePlan:
    """보이는 타일, 프리페치 타일, 프레임 y범위를 정한다 (동기, 렌더 스레드에서 호출)."""
    lattice = lattice_for(meta, spec.x_axis.column, settings.tile_size)
    visible = lattice.tiles_covering(view, zoom)
    around = lattice.with_neighbors(visible, prefetch_radius)
    visible_set = set(visible)
    prefetch = [tile for tile in around if tile not in visible_set]
    y_range = resolve_frame_y_range(meta, spec, spec.x_axis.column, spec_y_columns(spec), view)
    return FramePlan(lattice, visible, prefetch, y_range, dpr)


async def render_frame(
    meta: DatasetMeta,
    spec: ChartSpec,
    plan: FramePlan,
    renderer: TileRenderer | None = None,
) -> list[RenderedTile]:
    """프레임의 모든 타일(보이는 것 먼저)을 병렬로 그린다. 실패한 타일은 경고 후 뺀다."""
    renderer = renderer or default_tile_renderer
    tiles = [*plan.visible, *plan.prefetch]
    outcomes = await asyncio.gather(
        *(
            renderer.render(TileRequest(meta, spec, plan.lattice, tile, plan.dpr, plan.y_range))
            for tile in tiles
        ),
        return_exceptions=True,
    )
    rendered: list[RenderedTile] = []
    for tile, outcome in zip(tiles, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning(f"타일 {tile.tile_id} 렌더 실패: {outcome}")
            continue
        rendered.append(outcome)
    return rendered


def is_exact_spec(spec: ChartSpec) -> bool:
    """이 spec이 무왜곡 경로를 타는지. line + M4만 정확하다 (나머지는 Phase 2에서 확장)."""
    return spec.chart_type == ChartType.LINE and spec.downsample_algorithm == DownsampleAlgorithm.M4


# 타일 그림에 영향을 주는 spec 필드. 제목·크기·범례 등은 타일(축 없음)에 영향이 없어 빼서
# 크기가 다른 차트끼리도 같은 타일 캐시를 쓰게 한다.
_TILE_FIELDS = ("chart_type", "x_axis", "y_axis", "y_columns", "series", "background_color")


def render_hash(spec: ChartSpec, y_range: YRange, tile_size_px: int) -> str:
    """타일 그림을 결정하는 값들의 해시 (16자)."""
    payload = spec.model_dump(mode="json", include=set(_TILE_FIELDS))
    payload["y_range"] = [repr(y_range.low), repr(y_range.high)]
    payload["tile_size_px"] = tile_size_px
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
