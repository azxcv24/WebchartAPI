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
from webchart.domain.projection import SeriesWindow
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


def build_pyramid(
    store: RawStore,
    pyramid_dir: Path,
    x_column: str,
    y_columns: list[str],
    lattice_domain: XRange,
    max_level: int,
) -> int:
    """피라미드 레벨 0..max_level을 만들고 전체 파일 크기(바이트)를 반환한다."""
    if not y_columns:
        raise DatasetProcessingError("피라미드에는 y 컬럼이 하나 이상 필요하다")
    grid = BucketGrid.covering(lattice_domain, 1 << max_level)
    row_count = np.zeros(grid.bucket_count, dtype=np.int64)
    stats = {series: BucketStats.empty(grid.bucket_count) for series in y_columns}

    for batch in store.iter_row_groups([x_column, *y_columns]):
        _accumulate(batch, x_column, y_columns, grid, row_count, stats)

    if pyramid_dir.exists():
        shutil.rmtree(pyramid_dir)
    pyramid_dir.mkdir(parents=True)

    total_bytes = 0
    for level in range(max_level, -1, -1):
        total_bytes += _write_level(level_path(pyramid_dir, level), row_count, stats)
        if level > 0:
            row_count = row_count.reshape(-1, 2).sum(axis=1)
            stats = {series: level_stats.merge_pairs() for series, level_stats in stats.items()}
    return total_bytes


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
    table = pa.table(columns)
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

        레벨 버킷 하나는 시작 x가 속한 픽셀 열로 간다. 두 격자의 경계가 맞지 않으면 경계에
        걸친 버킷 하나만큼 오차가 생길 수 있다 (호출자가 exact=false로 표시한다).
        min/max 점은 실제 x로 그리므로 위치 오차는 없다.
        """
        bucket_count = level.grid.bucket_count
        first = int(np.clip(level.grid.index_of(np.array([grid.origin]))[0], 0, bucket_count))
        stop = int(np.clip(level.grid.index_of(np.array([grid.end]))[0] + 1, 0, bucket_count))
        regrouped = BucketStats.empty(grid.bucket_count)
        if stop > first:
            starts_x = level.grid.origin + np.arange(first, stop) * level.grid.bucket_width
            column = np.clip(grid.index_of(starts_x), 0, grid.bucket_count - 1)
            group_starts = np.flatnonzero(np.r_[True, column[1:] != column[:-1]])
            merged = level.stats(series, slice(first, stop)).regroup(group_starts)
            merge_into(regrouped, column[group_starts], merged)
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
