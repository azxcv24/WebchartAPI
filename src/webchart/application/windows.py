"""격자 하나에 대한 시리즈 집계(SeriesWindow)를 가져온다: 어느 저장소를 읽을지 정하는 곳.

경로 선택 (계획 문서 2.4)
1. 피라미드 레벨 격자가 요청 격자와 경계가 맞으면 → 피라미드 (정확)
   타일은 항상 여기에 해당한다 (L = z + log2(타일 px · dpr) ≤ 최대 레벨일 때).
2. 원본 읽기량이 예산 안이면 → 원본 즉석 집계 (정확)
3. 피라미드가 있으면 → 픽셀 폭의 1/8 이하 버킷 레벨을 픽셀 열로 다시 묶기 (근사, exact=false)
4. 그 외 → 원본 즉석 집계 (정확하지만 읽는 행 수에 비례해 느리다)

결과에는 정확 여부(exact)와 출처(source)가 붙어 응답 헤더까지 전달된다 (P3).
"""

import logging
import math
from dataclasses import dataclass
from pathlib import Path

from webchart.config import settings
from webchart.domain.geometry import BucketGrid, PixelLattice, TileAddress, XRange
from webchart.domain.projection import SeriesWindow
from webchart.infrastructure.pyramid_store import PyramidReader
from webchart.models.dataset import PYRAMID_FORMAT_VERSION, DatasetMeta, PyramidInfo
from webchart.services.data.access import raw_store

logger = logging.getLogger("webchart.application.windows")

# 피라미드 근사에서 픽셀 열 하나당 최소 버킷 수. 경계 오차를 열 폭의 1/8 이하로 줄인다.
_APPROX_BUCKETS_PER_COLUMN = 8

SOURCE_RAW = "raw"


@dataclass(frozen=True, slots=True)
class WindowSet:
    """시리즈별 집계 창과 그 출처.

    Attributes:
        windows: y 컬럼 이름 → 창 (요청한 순서)
        is_exact: 원본 전체로 그린 그림과 같음을 보증하는지
        source: "raw" 또는 "pyramid:L{레벨}"
    """

    windows: dict[str, SeriesWindow]
    is_exact: bool
    source: str

    def y_bounds(self) -> tuple[float, float] | None:
        """격자 안(이웃 제외) 모든 시리즈의 y 최솟값·최댓값."""
        bounds = [b for w in self.windows.values() if (b := w.stats.y_bounds()) is not None]
        if not bounds:
            return None
        return min(low for low, _ in bounds), max(high for _, high in bounds)


def lattice_for(meta: DatasetMeta, x_column: str, tile_size_px: int) -> PixelLattice:
    """데이터셋 x 컬럼의 전체 범위(정규 좌표)로 타일 격자를 만든다.

    통계가 없으면 [0, 1)로 대체한다 (타일 위치가 데이터와 맞지 않으므로 경고).
    """
    column = next((c for c in meta.columns if c.name == x_column), None)
    if column is not None and column.min_numeric is not None and column.max_numeric is not None:
        return PixelLattice.from_data_bounds(column.min_numeric, column.max_numeric, tile_size_px)
    logger.warning(
        f"x 컬럼 '{x_column}'의 수치 범위를 알 수 없어 [0, 1) 격자로 대체한다 "
        f"(dataset={meta.dataset_id})"
    )
    return PixelLattice(XRange(0.0, 1.0), tile_size_px)


def pyramid_dir(dataset_id: str, x_column: str) -> Path:
    return settings.data_dir / "datasets" / dataset_id / "pyramid" / x_column


def usable_pyramid(meta: DatasetMeta, x_column: str, y_columns: list[str]) -> PyramidReader | None:
    """지금 원본과 맞고 요청한 시리즈를 모두 담은 피라미드. 없으면 None."""
    info: PyramidInfo | None = meta.pyramids.get(x_column)
    if info is None or info.version != PYRAMID_FORMAT_VERSION:
        return None
    if info.data_version != meta.data_version:
        return None  # 재인제스트 이후의 낡은 피라미드
    if not set(y_columns) <= set(info.columns):
        return None
    try:
        return PyramidReader(pyramid_dir(meta.dataset_id, x_column), info)
    except (ValueError, OSError) as error:
        logger.warning(f"피라미드를 열 수 없어 원본을 쓴다: {error}")
        return None


def fetch_tile_windows(
    meta: DatasetMeta,
    x_column: str,
    y_columns: list[str],
    lattice: PixelLattice,
    tile: TileAddress,
    dpr: int,
) -> WindowSet:
    """타일 하나의 창. 타일 격자는 피라미드 레벨 격자의 일부라 어느 경로든 정확하다."""
    grid = lattice.tile_grid(tile, dpr)
    level_number = lattice.tile_level(tile.zoom, dpr)
    reader = usable_pyramid(meta, x_column, y_columns)
    if (
        reader is not None
        and level_number <= reader.info.max_level
        and reader.lattice.domain == lattice.domain
    ):
        level = reader.level(level_number)
        first = tile.index * grid.bucket_count
        windows = {s: reader.window(level, s, first, grid.bucket_count) for s in y_columns}
        return WindowSet(windows, is_exact=True, source=f"pyramid:L{level_number}")
    return _raw_windows(meta, x_column, y_columns, grid)


def fetch_view_windows(
    meta: DatasetMeta,
    x_column: str,
    y_columns: list[str],
    grid: BucketGrid,
    raw_row_budget: int | None = None,
) -> WindowSet:
    """임의의 x 구간·폭(정적 렌더, 뷰포트 y범위 계산)의 창. 경로 선택은 모듈 설명 참고."""
    budget = settings.exact_raw_row_budget if raw_row_budget is None else raw_row_budget
    reader = usable_pyramid(meta, x_column, y_columns)

    if reader is not None:
        aligned = _aligned_level(reader, grid)
        if aligned is not None:
            level = reader.level(aligned)
            windows = {s: reader.regrouped_window(level, s, grid) for s in y_columns}
            return WindowSet(windows, is_exact=True, source=f"pyramid:L{aligned}")

    store = raw_store(meta)
    if reader is None or store.estimate_rows(x_column, grid.x_range) <= budget:
        return _raw_windows(meta, x_column, y_columns, grid)

    level = reader.level(_approximation_level(reader, grid))
    windows = {s: reader.regrouped_window(level, s, grid) for s in y_columns}
    return WindowSet(windows, is_exact=False, source=f"pyramid:L{level.level}")


def _raw_windows(
    meta: DatasetMeta, x_column: str, y_columns: list[str], grid: BucketGrid
) -> WindowSet:
    windows = raw_store(meta).aggregate(x_column, y_columns, grid)
    return WindowSet(windows, is_exact=True, source=SOURCE_RAW)


def _aligned_level(reader: PyramidReader, grid: BucketGrid) -> int | None:
    """요청 격자의 모든 경계가 레벨 경계와 겹치는 가장 거친 레벨."""
    for level in range(reader.info.max_level + 1):
        if grid.alignment_ratio(reader.lattice.level_grid(level)) is not None:
            return level
    return None


def _approximation_level(reader: PyramidReader, grid: BucketGrid) -> int:
    """버킷 폭이 픽셀 열 폭의 1/8 이하가 되는 가장 거친 레벨 (없으면 최상세)."""
    target_width = grid.bucket_width / _APPROX_BUCKETS_PER_COLUMN
    wanted = math.ceil(math.log2(reader.lattice.domain.width / target_width))
    return max(0, min(reader.info.max_level, wanted))
