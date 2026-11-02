"""인터랙티브 차트용 타일 렌더러.

x 전체 범위를 `PixelLattice`로 2^zoom 등분하고, 뷰포트에 보이는 타일과
좌우 프리페치 타일만 그린다. 렌더된 타일은 Redis에 캐시한다.
"""

import asyncio
import logging

import pandas as pd

from webchart.cache import redis_cache
from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.domain.geometry import PixelLattice, TileAddress, XRange
from webchart.models.chart import ChartSpec
from webchart.models.dataset import DatasetMeta
from webchart.services.data.access import get_dataset_meta, read_window
from webchart.services.data.downsample import downsample
from webchart.services.data.pyramid import (
    level_for_zoom,
    read_pyramid_window,
    usable_pyramid,
)
from webchart.services.data.x_domain import to_canonical
from webchart.services.render.matplotlib_renderer import MatplotlibRenderer

logger = logging.getLogger("webchart.render.tile")

_renderer = MatplotlibRenderer()

# 프리페치: 보이는 타일 좌우로 미리 그려 둘 타일 수
PREFETCH_RADIUS = 2


def lattice_for(meta: DatasetMeta, x_column: str) -> PixelLattice:
    """데이터셋의 x 컬럼 통계(정규 좌표)로 타일 격자를 만든다.

    통계가 없으면 [0, 1) 격자로 대체한다. 이 경우 타일 위치가 실제 데이터와
    맞지 않으므로 경고를 남긴다.
    """
    column = next((c for c in meta.columns if c.name == x_column), None)
    if column is not None and column.min_numeric is not None and column.max_numeric is not None:
        return PixelLattice.from_data_bounds(
            column.min_numeric, column.max_numeric, settings.tile_size
        )
    logger.warning(
        f"x 컬럼 '{x_column}'의 수치 범위를 알 수 없어 [0, 1) 격자로 대체한다 "
        f"(dataset={meta.dataset_id})"
    )
    return PixelLattice(XRange(0.0, 1.0), settings.tile_size)


def _read_tile_data(
    dataset_id: str,
    meta: DatasetMeta,
    x_column: str,
    tile_range: XRange,
    columns: list[str],
    zoom: int,
):
    """타일 구간의 데이터를 피라미드에서 읽고, 쓸 수 없으면 원본에서 읽는다.

    반환하는 DataFrame의 x 컬럼은 정규 좌표(float64)로 바뀌어 있다. 타일은 축이 없으므로
    원래 타입(시간 등)이 필요 없고, 격자와 같은 단위여야 x 위치가 맞는다.

    렌더 스레드 풀에서 동기로 실행된다. 이 x 컬럼의 피라미드가 없거나, 줌이
    피라미드가 만들어진 해상도보다 깊으면 원본을 읽는다. 깊은 줌은 x 구간이
    좁아서 원본 읽기도 상대적으로 저렴하다는 가정이다 (D6: 현재는 전체 스캔이라
    이 가정이 성립하지 않으며 Phase 1.2에서 해결한다).
    """
    pyramid = usable_pyramid(meta, x_column)
    if pyramid is not None:
        level = level_for_zoom(pyramid, zoom)
        if level is not None:
            df = read_pyramid_window(dataset_id, x_column, level, tile_range.as_tuple(), columns)
            if df is not None:
                return _with_canonical_x(df, x_column)
    df = read_window(dataset_id, x_column, tile_range.as_tuple(), columns, meta=meta)
    return _with_canonical_x(df, x_column)


def _with_canonical_x(df: pd.DataFrame, x_column: str) -> pd.DataFrame:
    return df.assign(**{x_column: to_canonical(df[x_column])})


async def render_tile(
    dataset_id: str,
    chart_spec: ChartSpec,
    zoom: int,
    tile_x: int,
    tile_y: int = 0,
    chart_hash: str = "",
    meta: DatasetMeta | None = None,
) -> bytes:
    """타일 하나를 그린다. 캐시에 있으면 캐시를 반환한다.

    여러 타일을 연달아 그리는 호출자는 `meta`를 넘겨서 타일마다 meta.json을
    다시 읽지 않게 한다.
    """
    key = redis_cache.tile_key(dataset_id, chart_hash, zoom, tile_x, tile_y)
    try:
        cached = await redis_cache.get_tile(key)
        if cached is not None:
            return cached
    except Exception:
        logger.debug("Redis를 쓸 수 없어 캐시 없이 렌더한다")

    if meta is None:
        meta = get_dataset_meta(dataset_id)
    x_col = chart_spec.x_axis.column
    tile_range = lattice_for(meta, x_col).tile_range(TileAddress(zoom, tile_x))

    y_columns = _get_y_columns(chart_spec)
    columns = [x_col] + y_columns

    data = await run_in_thread(
        _read_tile_data, dataset_id, meta, x_col, tile_range, columns, zoom,
    )

    if not data.empty and len(data) > settings.tile_size:
        primary_y = y_columns[0] if y_columns else x_col
        data = await run_in_thread(
            downsample, data, x_col, primary_y, settings.tile_size, "lttb"
        )

    tile_bytes = await run_in_thread(
        _renderer.render_tile,
        data,
        x_col,
        y_columns,
        chart_spec.chart_type,
        settings.tile_size,
        tile_range.as_tuple(),
    )

    try:
        await redis_cache.set_tile(key, tile_bytes)
    except Exception:
        logger.debug("타일 캐시 저장 실패")

    return tile_bytes


async def render_visible_tiles(
    dataset_id: str,
    chart_spec: ChartSpec,
    viewport_x_range: tuple[float, float],
    zoom: int,
    chart_hash: str = "",
    prefetch: int = PREFETCH_RADIUS,
) -> dict[str, bytes]:
    """뷰포트에 보이는 타일과 프리페치 타일을 그린다.

    반환값은 `tile_id → PNG 바이트`이다. 실패한 타일은 경고만 남기고 빠진다.
    """
    meta = get_dataset_meta(dataset_id)
    lattice = lattice_for(meta, chart_spec.x_axis.column)

    try:
        view = XRange(*viewport_x_range)
    except ValueError:
        logger.warning(f"잘못된 뷰포트 x 구간 {viewport_x_range}: 타일을 그리지 않는다")
        return {}
    visible = lattice.tiles_covering(view, zoom)
    targets = lattice.with_neighbors(visible, prefetch)

    # render_tile 내부의 run_in_thread가 렌더 풀로 작업을 넘기므로,
    # 여기서 gather하면 타일들이 병렬로 그려진다.
    rendered = await asyncio.gather(
        *(
            render_tile(dataset_id, chart_spec, tile.zoom, tile.index, 0, chart_hash, meta=meta)
            for tile in targets
        ),
        return_exceptions=True,
    )

    tiles: dict[str, bytes] = {}
    for tile, outcome in zip(targets, rendered):
        if isinstance(outcome, BaseException):
            logger.warning(f"타일 {tile.tile_id} 렌더 실패: {outcome}")
            continue
        tiles[tile.tile_id] = outcome
    return tiles


def _get_y_columns(spec: ChartSpec) -> list[str]:
    """차트 spec에서 y 컬럼 이름 목록을 꺼낸다."""
    columns = []
    if spec.series:
        columns = [s.column for s in spec.series]
    elif spec.y_columns:
        columns = spec.y_columns
    elif spec.y_axis:
        columns = [spec.y_axis.column]
    return columns
