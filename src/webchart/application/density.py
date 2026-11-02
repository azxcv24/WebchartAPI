"""2D 밀도 격자 가져오기 (scatter, heatmap, 계획 문서 2.6).

경로 선택은 windows.py와 같은 원칙이다.
1. 밀도 피라미드가 있고 요청 격자와 정렬되면(열 경계·y범위·행 수) → 피라미드 (정확)
2. 원본 읽기량이 예산 안이거나 피라미드가 없으면 → 원본을 로우그룹씩 읽어 센다 (정확)
3. 그 외 → 피라미드 셀을 픽셀로 다시 묶는다 (근사, exact=false)
"""

import math
from dataclasses import dataclass

from webchart.application.windows import SOURCE_RAW, usable_pyramid
from webchart.config import settings
from webchart.domain.geometry import BucketGrid, PixelLattice, TileAddress
from webchart.domain.projection import DensityGrid, add_points, empty_density
from webchart.domain.y_range import YRange
from webchart.infrastructure.pyramid_store import PyramidReader
from webchart.models.dataset import DatasetMeta
from webchart.services.data.access import raw_store


@dataclass(frozen=True, slots=True)
class DensityResult:
    density: DensityGrid
    is_exact: bool
    source: str


def fetch_tile_density(
    meta: DatasetMeta,
    x_column: str,
    y_column: str,
    lattice: PixelLattice,
    tile: TileAddress,
    dpr: int,
    y_range: YRange,
) -> DensityResult:
    grid = lattice.tile_grid(tile, dpr)
    return fetch_view_density(meta, x_column, y_column, grid, y_range, rows=grid.bucket_count)


def fetch_view_density(
    meta: DatasetMeta,
    x_column: str,
    y_column: str,
    grid: BucketGrid,
    y_range: YRange,
    rows: int,
    raw_row_budget: int | None = None,
) -> DensityResult:
    budget = settings.exact_raw_row_budget if raw_row_budget is None else raw_row_budget
    reader = usable_pyramid(meta, x_column, [y_column])
    level = _density_level(reader, y_column, grid) if reader is not None else None

    if reader is not None and level is not None:
        density, is_exact = reader.density(level, y_column, grid, *y_range.as_tuple(), rows)
        if is_exact:
            return DensityResult(density, True, f"pyramid:D{level}")

    store = raw_store(meta)
    if reader is None or level is None or store.estimate_rows(x_column, grid.x_range) <= budget:
        return DensityResult(_raw_density(meta, x_column, y_column, grid, y_range, rows), True,
                             SOURCE_RAW)  # fmt: skip
    return DensityResult(density, False, f"pyramid:D{level}")


def _density_level(reader: PyramidReader, y_column: str, grid: BucketGrid) -> int | None:
    """열 폭 이하 셀을 가진 가장 거친 밀도 레벨 (피라미드 상한을 넘으면 최상세)."""
    if not reader.has_density(y_column):
        return None
    assert reader.info.density_max_level is not None
    wanted = math.ceil(math.log2(reader.lattice.domain.width / grid.bucket_width) - 1e-9)
    return max(0, min(reader.info.density_max_level, wanted))


def _raw_density(
    meta: DatasetMeta,
    x_column: str,
    y_column: str,
    grid: BucketGrid,
    y_range: YRange,
    rows: int,
) -> DensityGrid:
    density = empty_density(grid, y_range.low, y_range.high, rows)
    for x, values in raw_store(meta).scan(x_column, [y_column], grid.x_range):
        add_points(density, x, values[y_column])
    return density
