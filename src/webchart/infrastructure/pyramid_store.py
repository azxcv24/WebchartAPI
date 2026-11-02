"""피라미드 저장소 v2: 격자 정렬 BucketStats 레벨 (계획 문서 2.2~2.4, 2.8).

파일 구조: `datasets/{id}/pyramid/{x_column}/L{level}.arrow`
- 레벨 하나 = Arrow IPC 파일 하나 (압축 없음). 행 i = 버킷 i (조밀 배열).
- 컬럼: `row_count`(버킷의 행 수, 결측 y 포함) + 시리즈마다 `{시리즈}:{BucketStats 필드}`.
- 읽을 때 mmap으로 열어 파이썬 힙 밖의 OS 페이지 캐시를 그대로 쓴다 (제로카피).
  서버 RAM이 크면 자주 쓰는 레벨이 자연히 메모리에 상주한다 (P6).

빌드: 원본 로우그룹을 하나씩 읽어 최상세 레벨에 merge하고(메모리 = 최상세 레벨 + 로우그룹 1개),
상위 레벨은 아래 레벨 버킷을 2개씩 merge해 만든다. BucketStats가 모노이드라 결과가 정확하다.
"""

import logging
import math
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa

from webchart.core.errors import DatasetProcessingError
from webchart.domain.bucket_stats import (
    BucketStats,
    IntArray,
    aggregate_sorted,
    merge_into,
)
from webchart.domain.geometry import BucketGrid, PixelLattice, XRange
from webchart.domain.projection import DensityGrid, SeriesWindow, add_points, empty_density
from webchart.infrastructure.raw_store import RawStore
from webchart.models.dataset import PyramidInfo
from webchart.services.data.x_domain import canonical_from_arrow

logger = logging.getLogger("webchart.infrastructure.pyramid_store")

_STAT_FIELDS = tuple(BucketStats.__dataclass_fields__)
_ROW_COUNT = "row_count"

# 버킷 하나·시리즈 하나의 크기 (BucketStats 필드 12개 × 8바이트)
_BYTES_PER_SERIES_BUCKET = 8 * len(_STAT_FIELDS)

# 이웃 버킷을 찾을 때 처음 훑는 폭. 못 찾으면 4배씩 넓힌다.
_NEIGHBOR_SEARCH_START = 1024


def stat_column(series: str, field: str) -> str:
    return f"{series}:{field}"


def level_path(pyramid_dir: Path, level: int) -> Path:
    return pyramid_dir / f"L{level}.arrow"


# 최상세 레벨 버킷 하나에 들어가는 평균 행 수의 하한. 버킷(시리즈당 96바이트)이 원본 행보다
# 훨씬 크므로, 버킷당 행이 적으면 피라미드가 원본보다 커진다. 그보다 깊은 줌은 원본을 읽어도
# 범위가 좁아 싸다 (zone map 범위 읽기).
MIN_ROWS_PER_FINEST_BUCKET = 16


def choose_max_level(row_count: int, series_count: int, max_level: int, max_bytes: int) -> int:
    """최상세 레벨을 고른다.

    - 버킷당 평균 MIN_ROWS_PER_FINEST_BUCKET행 이상이 되게 한다 (피라미드 < 원본 크기)
    - 모든 레벨의 합(최상세의 약 2배)이 max_bytes 안에 들어오게 한다
    - 설정 상한 max_level을 넘지 않는다
    """
    bytes_per_bucket = 8 + _BYTES_PER_SERIES_BUCKET * series_count
    by_budget = int(math.floor(math.log2(max(1, max_bytes // (2 * bytes_per_bucket)))))
    by_rows = int(math.floor(math.log2(max(1, row_count // MIN_ROWS_PER_FINEST_BUCKET))))
    return max(0, min(max_level, by_budget, by_rows))


@dataclass(frozen=True, slots=True)
class DensityPlan:
    """2D 밀도 피라미드 명세 (scatter, heatmap, 계획 문서 2.6).

    레벨 L은 x를 2^L 열, y를 `rows`행으로 나눈 점 개수 격자다. y 범위는 시리즈마다
    전역 자동 범위(GLOBAL 정책의 y범위와 같다)라, 그 범위로 그리는 타일만 정확히 쓸 수 있다.
    rows가 타일 높이(px · dpr)의 배수라 행을 묶으면 타일 픽셀 행과 정확히 맞는다.
    """

    max_level: int
    rows: int
    y_ranges: dict[str, tuple[float, float]]


def build_pyramid(
    store: RawStore,
    pyramid_dir: Path,
    x_column: str,
    y_columns: list[str],
    lattice_domain: XRange,
    max_level: int,
    density: DensityPlan | None = None,
) -> int:
    """피라미드 레벨 0..max_level(과 밀도 레벨)을 만들고 전체 파일 크기(바이트)를 반환한다."""
    if not y_columns:
        raise DatasetProcessingError("피라미드에는 y 컬럼이 하나 이상 필요하다")
    grid = BucketGrid.covering(lattice_domain, 1 << max_level)
    row_count = np.zeros(grid.bucket_count, dtype=np.int64)
    stats = {series: BucketStats.empty(grid.bucket_count) for series in y_columns}
    densities = (
        {
            series: empty_density(
                BucketGrid.covering(lattice_domain, 1 << density.max_level),
                *density.y_ranges[series],
                rows=density.rows,
            )
            for series in density.y_ranges
        }
        if density
        else {}
    )

    for batch in store.iter_row_groups([x_column, *y_columns]):
        _accumulate(batch, x_column, y_columns, grid, row_count, stats)
        _accumulate_density(batch, x_column, densities)

    if pyramid_dir.exists():
        shutil.rmtree(pyramid_dir)
    pyramid_dir.mkdir(parents=True)

    total_bytes = 0
    for level in range(max_level, -1, -1):
        total_bytes += _write_level(level_path(pyramid_dir, level), row_count, stats)
        if level > 0:
            row_count = row_count.reshape(-1, 2).sum(axis=1)
            stats = {series: level_stats.merge_pairs() for series, level_stats in stats.items()}

    # 밀도: counts[행, 열] → 열 우선(열마다 행 rows개가 연속)으로 저장해 x 구간을 연속으로 자른다
    columns_major = {series: d.counts.T.copy() for series, d in densities.items()}
    for level in range(density.max_level if density else -1, -1, -1):
        table = pa.table(
            {f"{series}:density": counts.reshape(-1).astype(np.uint32)
             for series, counts in columns_major.items()}
        )  # fmt: skip
        total_bytes += _write_table(density_level_path(pyramid_dir, level), table)
        if level > 0:
            columns_major = {
                series: counts.reshape(-1, 2, counts.shape[1]).sum(axis=1)
                for series, counts in columns_major.items()
            }
    return total_bytes


def density_level_path(pyramid_dir: Path, level: int) -> Path:
    return pyramid_dir / f"density_L{level}.arrow"


def _accumulate_density(batch: pa.Table, x_column: str, densities: dict[str, DensityGrid]) -> None:
    if not densities:
        return
    x = canonical_from_arrow(batch.column(x_column))
    for series, density in densities.items():
        y = canonical_from_arrow(batch.column(series))
        add_points(density, x, y)


def _accumulate(
    batch: pa.Table,
    x_column: str,
    y_columns: list[str],
    grid: BucketGrid,
    row_count: IntArray,
    stats: dict[str, BucketStats],
) -> None:
    """로우그룹 하나를 최상세 레벨에 합친다."""
    x = canonical_from_arrow(batch.column(x_column))
    has_x = ~np.isnan(x)
    order = np.flatnonzero(has_x)
    if len(order) == 0:
        return
    x = x[order]
    if np.any(np.diff(x) < 0):  # 정렬 컬럼이 아닌 x로 빌드할 때
        sorted_positions = np.argsort(x, kind="stable")
        order, x = order[sorted_positions], x[sorted_positions]
    # 격자 끝은 x 최댓값 바로 다음 float라 모든 점이 안에 들지만, 나눗셈 반올림 대비로 자른다
    index = np.clip(grid.index_of(x), 0, grid.bucket_count - 1)

    buckets, rows_per_bucket = np.unique(index, return_counts=True)
    row_count[buckets] += rows_per_bucket
    for series in y_columns:
        y = canonical_from_arrow(batch.column(series))[order]
        ids, part = aggregate_sorted(x, y, index)
        merge_into(stats[series], ids, part)


def _write_level(path: Path, row_count: IntArray, stats: dict[str, BucketStats]) -> int:
    columns: dict[str, npt.NDArray[np.generic]] = {_ROW_COUNT: row_count}
    for series, level_stats in stats.items():
        for field in _STAT_FIELDS:
            columns[stat_column(series, field)] = getattr(level_stats, field)
    return _write_table(path, pa.table(columns))


def _write_table(path: Path, table: pa.Table) -> int:
    with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return path.stat().st_size


# --- 읽기 -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PyramidLevel:
    """mmap으로 연 피라미드 레벨 하나."""

    level: int
    grid: BucketGrid
    table: pa.Table

    @property
    def row_count(self) -> IntArray:
        return self._column(_ROW_COUNT)

    def stats(self, series: str, buckets: slice) -> BucketStats:
        """버킷 구간의 집계. mmap 버퍼의 뷰라 복사하지 않는다."""
        return BucketStats(
            **{field: self._column(stat_column(series, field))[buckets] for field in _STAT_FIELDS}
        )

    def has_series(self, series: str) -> bool:
        return stat_column(series, "count") in self.table.column_names

    def _column(self, name: str) -> npt.NDArray[Any]:
        values: npt.NDArray[Any] = self.table.column(name).chunk(0).to_numpy(zero_copy_only=True)
        return values


class PyramidReader:
    """피라미드 하나(데이터셋 × x 컬럼)의 레벨 읽기."""

    def __init__(self, pyramid_dir: Path, info: PyramidInfo) -> None:
        if info.domain_start is None or info.domain_end is None:
            raise ValueError("피라미드 정보에 격자 범위가 없다")
        self._dir = pyramid_dir
        self.info = info
        # 격자는 타일 크기와 무관하므로 tile_size_px는 아무 2의 거듭제곱이면 된다
        self.lattice = PixelLattice(XRange(info.domain_start, info.domain_end), tile_size_px=1)

    def level(self, level: int) -> PyramidLevel:
        if not 0 <= level <= self.info.max_level:
            raise ValueError(f"레벨 {level}은 0..{self.info.max_level} 범위 밖이다")
        table = _open_level_table(level_path(self._dir, level))
        return PyramidLevel(level, self.lattice.level_grid(level), table)

    def has_density(self, series: str) -> bool:
        return self.info.density_max_level is not None and series in self.info.density_y_ranges

    def density(
        self, level: int, series: str, grid: BucketGrid, y_low: float, y_high: float, rows: int
    ) -> tuple[DensityGrid, bool]:
        """밀도 레벨을 요청 격자(열 grid × 행 rows, y [y_low, y_high])로 다시 묶는다.

        반환: (밀도 격자, 정확 여부). 열 경계가 레벨 경계와 맞고, y범위가 피라미드와 같고,
        피라미드 행 수가 rows의 배수이면 정확하다. 아니면 셀 시작 위치 기준으로 배정하는
        근사다 (경계에 걸친 셀 하나만큼 오차).
        """
        info = self.info
        assert info.density_max_level is not None
        level_grid = self.lattice.level_grid(level)
        stored_low, stored_high = info.density_y_ranges[series]
        stored_rows = info.density_rows
        values = (
            _open_level_table(density_level_path(self._dir, level))
            .column(f"{series}:density")
            .chunk(0)
            .to_numpy(zero_copy_only=True)
        )

        first = int(
            np.clip(level_grid.index_of(np.array([grid.origin]))[0], 0, level_grid.bucket_count)
        )
        stop = int(
            np.clip(level_grid.index_of(np.array([grid.end]))[0] + 1, 0, level_grid.bucket_count)
        )
        cells = values[first * stored_rows : stop * stored_rows].reshape(-1, stored_rows)

        result = empty_density(grid, y_low, y_high, rows)
        if len(cells):
            # 셀 중심으로 배정한다. 시작 좌표는 경계에 딱 걸려 부동소수 반올림으로 앞 칸에
            # 들어갈 수 있지만, 중심은 경계에서 반 칸 떨어져 있어 정렬된 경우 항상 정확하다.
            cell_x = level_grid.origin + (np.arange(first, stop) + 0.5) * level_grid.bucket_width
            column = grid.index_of(cell_x)
            valid_column = (column >= 0) & (column < grid.bucket_count)
            row_height = (stored_high - stored_low) / stored_rows
            row = result.row_index_of(stored_low + (np.arange(stored_rows) + 0.5) * row_height)
            valid_row = (row >= 0) & (row < rows)
            target = row[None, valid_row] * grid.bucket_count + column[valid_column, None]
            flat = np.bincount(
                target.reshape(-1),
                weights=cells[valid_column][:, valid_row].reshape(-1).astype(np.float64),
                minlength=result.counts.size,
            )
            result.counts.reshape(-1)[:] = np.rint(flat).astype(np.int64)

        is_exact = (
            grid.alignment_ratio(level_grid) is not None
            and (stored_low, stored_high) == (y_low, y_high)
            and stored_rows % rows == 0
        )
        return result, is_exact

    def window(self, level: PyramidLevel, series: str, first: int, count: int) -> SeriesWindow:
        """레벨의 버킷 `[first, first + count)`를 그대로 격자로 쓰는 창 (타일용, 정확)."""
        grid = level.grid.sub_grid(first, count)
        lead, trail = _neighbor_buckets(level, first, first + count)
        return SeriesWindow(
            grid=grid,
            stats=level.stats(series, slice(first, first + count)),
            lead=_single(level, series, lead),
            trail=_single(level, series, trail),
        )

    def regrouped_window(self, level: PyramidLevel, series: str, grid: BucketGrid) -> SeriesWindow:
        """레벨 버킷을 임의의 격자(픽셀 열)로 다시 묶은 창 (정적 렌더용).

        레벨 버킷 하나는 중심 x가 속한 픽셀 열로 간다. 두 격자의 경계가 맞지 않으면 경계에
        걸친 버킷 하나만큼 오차가 생길 수 있다 (호출자가 exact=false로 표시한다).
        min/max 점은 실제 x로 그리므로 위치 오차는 없다.
        """
        bucket_count = level.grid.bucket_count
        first = int(np.clip(level.grid.index_of(np.array([grid.origin]))[0], 0, bucket_count))
        stop = int(np.clip(level.grid.index_of(np.array([grid.end]))[0] + 1, 0, bucket_count))
        regrouped = BucketStats.empty(grid.bucket_count)
        if stop > first:
            # 버킷 중심으로 배정한다 (density와 같은 이유: 경계 반올림 방지)
            centers_x = level.grid.origin + (np.arange(first, stop) + 0.5) * level.grid.bucket_width
            column = grid.index_of(centers_x)
            # 중심이 격자 밖인 버킷(양 끝에 걸친 버킷)은 이웃 열에 속하므로 뺀다
            inside = np.flatnonzero((column >= 0) & (column < grid.bucket_count))
            if len(inside):
                low, high = int(inside[0]), int(inside[-1]) + 1
                column = column[low:high]
                group_starts = np.flatnonzero(np.r_[True, column[1:] != column[:-1]])
                stats = level.stats(series, slice(first + low, first + high))
                merge_into(regrouped, column[group_starts], stats.regroup(group_starts))
                first, stop = first + low, first + high
        # 이웃은 실제로 쓴 버킷의 바깥에서 찾는다 (뺀 양 끝 버킷이 이웃이 될 수 있다)
        lead, trail = _neighbor_buckets(level, first, stop)
        return SeriesWindow(
            grid=grid,
            stats=regrouped,
            lead=_single(level, series, lead),
            trail=_single(level, series, trail),
        )


def _single(level: PyramidLevel, series: str, bucket: int | None) -> BucketStats:
    if bucket is None:
        return BucketStats.empty(0)
    return level.stats(series, slice(bucket, bucket + 1))


def _neighbor_buckets(level: PyramidLevel, first: int, stop: int) -> tuple[int | None, int | None]:
    """[first, stop) 바깥에서 행이 있는 가장 가까운 버킷 (왼쪽, 오른쪽)."""
    rows = level.row_count
    return _search(rows, first, step=-1), _search(rows, stop, step=1)


def _search(rows: IntArray, boundary: int, step: int) -> int | None:
    """boundary 바깥 방향으로 행이 있는 첫 버킷. 폭을 4배씩 넓혀 가며 찾는다."""
    width = _NEIGHBOR_SEARCH_START
    while True:
        if step < 0:
            low = max(0, boundary - width)
            found = np.flatnonzero(rows[low:boundary])
            if len(found):
                return low + int(found[-1])
            if low == 0:
                return None
        else:
            high = min(len(rows), boundary + width)
            found = np.flatnonzero(rows[boundary:high])
            if len(found):
                return boundary + int(found[0])
            if high == len(rows):
                return None
        width *= 4


# 열린 레벨 파일 캐시. mmap은 가볍지만 파일을 매번 다시 여는 비용을 아낀다.
# 키에 수정 시각을 넣어 재빌드된 파일은 새로 연다 (이전 mmap은 이전 inode를 계속 가리켜 안전).
_MAX_OPEN_LEVELS = 256
_open_levels: OrderedDict[tuple[str, int], pa.Table] = OrderedDict()
_open_levels_lock = threading.Lock()


def _open_level_table(path: Path) -> pa.Table:
    key = (str(path), path.stat().st_mtime_ns)
    with _open_levels_lock:
        table = _open_levels.get(key)
        if table is not None:
            _open_levels.move_to_end(key)
            return table
    # mmap을 닫지 않는다: 테이블 버퍼가 매핑을 직접 가리키므로 테이블이 살아 있는 동안 유지된다
    table = pa.ipc.open_file(pa.memory_map(str(path), "r")).read_all()
    with _open_levels_lock:
        _open_levels[key] = table
        while len(_open_levels) > _MAX_OPEN_LEVELS:
            _open_levels.popitem(last=False)
    return table
