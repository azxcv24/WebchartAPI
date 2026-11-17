"""피라미드를 임의의 픽셀 격자로 정확하게 다시 묶는다 (정적 렌더, 계획 문서 10장 "남은 일").

정적 렌더는 x범위·폭이 자유로워 픽셀 열 경계가 피라미드 버킷 경계와 맞지 않는다.
경계에 걸친 버킷의 행은 두 열로 나뉘어야 하는데, 버킷 집계만으로는 어디서 나뉘는지 모른다.

방법
1. 최상세 레벨(F)에서 각 열 경계 주변 버킷 3개를 "경계 버킷"으로 둔다. 나머지 버킷은
   경계에서 한 버킷 이상 떨어져 있으므로 어느 열에 속하는지 확실하다.
2. 경계 버킷 사이의 연속 구간은 레벨을 올라가며 2진 블록으로 쪼개 읽는다 (구간 하나에
   레벨마다 버킷 2개 이하). 넓은 열도 몇십 개 버킷만 읽는다.
3. 경계 버킷은 담긴 행의 실제 x 범위(first/last, 결측 행 x)로 판정한다.
   - 한 열 안에 다 들어가면 버킷 통째로 그 열에 합친다 (원본을 읽지 않는다).
   - 두 열에 걸치면 그 버킷의 원본 행만 읽어 열별로 집계한다 (읽을 로우그룹 행 수가
     예산 안일 때). 예산을 넘으면 버킷 중심이 속한 열에 합치고 exact=false로 표시한다.

데이터가 성기면(최상세 버킷 하나에 행이 1개 이하) 걸치는 버킷이 없어 원본을 전혀 읽지
않고도 정확하다. 걸치는 버킷이 남아도 오차는 최상세 버킷 하나 폭 이내로 줄어든다.
열 배정 규칙은 원본 즉석 집계(RawStore.aggregate)와 같다.
"""

from dataclasses import dataclass

import numpy as np

from webchart.domain.bucket_stats import BucketStats, aggregate_sorted, merge_into
from webchart.domain.geometry import BucketGrid, XRange
from webchart.domain.projection import SeriesWindow
from webchart.infrastructure.pyramid_store import (
    PyramidLevel,
    PyramidReader,
    nearest_filled_bucket,
)
from webchart.infrastructure.raw_store import RawStore

IntArray = np.ndarray
FloatArray = np.ndarray


@dataclass(frozen=True, slots=True)
class RegroupedViews:
    windows: dict[str, SeriesWindow]
    is_exact: bool
    source: str


def regroup_view(
    reader: PyramidReader,
    store: RawStore,
    x_column: str,
    y_columns: list[str],
    grid: BucketGrid,
    raw_row_budget: int,
) -> RegroupedViews:
    """grid의 열별 집계를 피라미드(+ 경계에 걸친 버킷의 원본)로 만든다. 모듈 설명 참고."""
    finest = reader.level(reader.info.max_level)
    edges = _edge_buckets(finest.grid, grid)
    columns = {name: BucketStats.empty(grid.bucket_count) for name in y_columns}
    leads = {name: BucketStats.empty(1) for name in y_columns}
    trails = {name: BucketStats.empty(1) for name in y_columns}

    _merge_interior_runs(reader, finest, edges, grid, columns)

    straddlers = _straddling(finest, edges, y_columns, grid)
    settled = edges[~np.isin(edges, straddlers)]
    for name in y_columns:
        stats = finest.stats(name, settled)
        _route(grid, _span_column(grid, stats), stats, columns[name], leads[name], trails[name])

    is_exact = True
    used_raw = False
    if len(straddlers):
        groups = _raw_groups(store, x_column, finest.grid, straddlers)
        if int(store.zone_map(x_column).row_counts[groups].sum()) <= raw_row_budget:
            _merge_raw_rows(store, x_column, y_columns, finest.grid, straddlers, groups, grid,
                            columns, leads, trails)  # fmt: skip
            used_raw = True
        else:
            # 예산 초과: 걸친 버킷은 중심이 속한 열로 보낸다 (오차는 최상세 버킷 하나 폭 이내)
            centers = finest.grid.origin + (straddlers + 0.5) * finest.grid.bucket_width
            for name in y_columns:
                stats = finest.stats(name, straddlers)
                _route(grid, _column_of(grid, centers), stats, columns[name], leads[name],
                       trails[name])  # fmt: skip
            is_exact = False

    windows = {
        name: SeriesWindow(
            grid=grid,
            stats=columns[name],
            lead=_nearest_outside(finest, name, leads[name], int(edges[0]), step=-1),
            trail=_nearest_outside(finest, name, trails[name], int(edges[-1]) + 1, step=1),
        )
        for name in y_columns
    }
    source = f"pyramid:L{finest.level}" + ("+raw" if used_raw else "")
    return RegroupedViews(windows, is_exact, source)


# --- 경계 버킷과 내부 구간 ------------------------------------------------------


def _edge_buckets(level_grid: BucketGrid, grid: BucketGrid) -> IntArray:
    """열 경계마다 그 경계를 담은 최상세 버킷과 양옆 버킷 (오름차순, 중복 없음)."""
    boundaries = grid.origin + np.arange(grid.bucket_count + 1) * grid.bucket_width
    center = level_grid.index_of(boundaries)
    around = np.concatenate([center - 1, center, center + 1])
    return np.unique(np.clip(around, 0, level_grid.bucket_count - 1))


def _merge_interior_runs(
    reader: PyramidReader,
    finest: PyramidLevel,
    edges: IntArray,
    grid: BucketGrid,
    columns: dict[str, BucketStats],
) -> None:
    """경계 버킷 사이의 연속 구간을 그 구간이 속한 열에 합친다 (2진 블록으로 쪼개 읽는다)."""
    run_lo = edges[:-1] + 1
    run_hi = edges[1:]
    has_run = run_hi > run_lo
    run_lo, run_hi = run_lo[has_run], run_hi[has_run]
    middle_x = finest.grid.origin + (run_lo + run_hi) / 2 * finest.grid.bucket_width
    run_column = _column_of(grid, middle_x)
    inside = (run_column >= 0) & (run_column < grid.bucket_count)
    lo, hi, run_column = run_lo[inside], run_hi[inside], run_column[inside]

    for level_number in range(finest.level, -1, -1):
        if not np.any(lo < hi):
            return
        level = reader.level(level_number)
        take_lo = (lo < hi) & (lo % 2 == 1)
        _merge_blocks(level, lo[take_lo], run_column[take_lo], columns)
        lo = lo + take_lo
        take_hi = (lo < hi) & (hi % 2 == 1)
        _merge_blocks(level, hi[take_hi] - 1, run_column[take_hi], columns)
        hi = hi - take_hi
        lo, hi = lo // 2, hi // 2


def _merge_blocks(
    level: PyramidLevel, buckets: IntArray, at: IntArray, columns: dict[str, BucketStats]
) -> None:
    if len(buckets) == 0:
        return
    for name, target in columns.items():
        _merge_repeated(target, at, level.stats(name, buckets))


def _merge_repeated(target: BucketStats, at: IntArray, part: BucketStats) -> None:
    """merge_into와 같지만 at에 같은 번호가 여러 번 나와도 된다 (번호별로 차례로 합친다)."""
    order = np.argsort(at, kind="stable")
    sorted_at = at[order]
    starts = np.r_[True, sorted_at[1:] != sorted_at[:-1]]
    group_start = np.maximum.accumulate(np.where(starts, np.arange(len(at)), 0))
    rank = np.arange(len(at)) - group_start
    for r in range(int(rank.max(initial=-1)) + 1):
        picked = order[rank == r]
        merge_into(target, at[picked], part.take(picked))


# --- 열 배정 (RawStore.aggregate와 같은 규칙) -----------------------------------


def _column_of(grid: BucketGrid, x: FloatArray) -> IntArray:
    """x가 속한 열. 격자 앞이면 -1, 뒤면 bucket_count."""
    column = np.clip(grid.index_of(x), 0, grid.bucket_count - 1)
    column = np.where(x < grid.x_range.start, -1, column)
    return np.where(x >= grid.x_range.end, grid.bucket_count, column)


def _content_span(stats: BucketStats) -> tuple[FloatArray, FloatArray]:
    """버킷에 담긴 행(결측 포함)의 x 최솟값·최댓값. 빈 버킷은 (inf, -inf)."""
    low = np.fmin(stats.first_x, stats.gap_first_x)
    high = np.fmax(stats.last_x, stats.gap_last_x)
    return low, high


def _span_column(grid: BucketGrid, stats: BucketStats) -> IntArray:
    """한 열 안에 다 들어가는 버킷의 열 (빈 버킷은 -2, 버리라는 뜻)."""
    low, _ = _content_span(stats)
    column = _column_of(grid, np.where(np.isfinite(low), low, grid.x_range.start))
    return np.where(np.isfinite(low), column, -2)


def _straddling(
    finest: PyramidLevel, edges: IntArray, y_columns: list[str], grid: BucketGrid
) -> IntArray:
    """행이 두 열(또는 격자 안팎)에 걸친 경계 버킷. 시리즈 하나라도 걸치면 걸친 것으로 본다."""
    crossing = np.zeros(len(edges), dtype=bool)
    for name in y_columns:
        low, high = _content_span(finest.stats(name, edges))
        filled = np.isfinite(low)
        low_column = _column_of(grid, np.where(filled, low, 0.0))
        high_column = _column_of(grid, np.where(filled, high, 0.0))
        crossing |= filled & (low_column != high_column)
    return edges[crossing]


def _route(
    grid: BucketGrid,
    column: IntArray,
    stats: BucketStats,
    columns: BucketStats,
    lead: BucketStats,
    trail: BucketStats,
) -> None:
    """버킷들을 열(안), lead(앞), trail(뒤)로 보낸다. column이 -2인 버킷은 버린다."""
    inside = (column >= 0) & (column < grid.bucket_count)
    if inside.any():
        _merge_repeated(columns, column[inside], stats.take(np.flatnonzero(inside)))
    for target, picked in ((lead, column == -1), (trail, column == grid.bucket_count)):
        if picked.any():
            _merge_repeated(target, np.zeros(int(picked.sum()), dtype=np.int64),
                            stats.take(np.flatnonzero(picked)))  # fmt: skip


# --- 걸친 버킷의 원본 -----------------------------------------------------------


def _raw_groups(
    store: RawStore, x_column: str, level_grid: BucketGrid, straddlers: IntArray
) -> IntArray:
    """걸친 버킷들의 행이 들어 있을 수 있는 로우그룹 (양옆으로 버킷 하나씩 여유를 둔다)."""
    zone_map = store.zone_map(x_column)
    width = level_grid.bucket_width
    groups = [
        zone_map.overlapping(
            XRange(level_grid.origin + (b - 1) * width, level_grid.origin + (b + 2) * width)
        )  # fmt: skip
        for b in straddlers
    ]
    return np.unique(np.concatenate(groups)) if groups else np.empty(0, dtype=np.int64)


def _merge_raw_rows(
    store: RawStore,
    x_column: str,
    y_columns: list[str],
    level_grid: BucketGrid,
    straddlers: IntArray,
    groups: IntArray,
    grid: BucketGrid,
    columns: dict[str, BucketStats],
    leads: dict[str, BucketStats],
    trails: dict[str, BucketStats],
) -> None:
    """걸친 버킷에 속한 원본 행(피라미드 빌드와 같은 버킷 배정)을 열별로 집계해 합친다."""
    for x, values in store.read_groups(x_column, y_columns, groups):
        has_x = ~np.isnan(x)
        bucket = np.clip(
            level_grid.index_of(np.where(has_x, x, 0.0)), 0, level_grid.bucket_count - 1
        )
        rows = np.flatnonzero(has_x & np.isin(bucket, straddlers))
        if len(rows) == 0:
            continue
        rows = rows[np.argsort(x[rows], kind="stable")]
        column = _column_of(grid, x[rows])
        for name in y_columns:
            ids, part = aggregate_sorted(x[rows], values[name][rows], column)
            _route(grid, ids, part, columns[name], leads[name], trails[name])


def _nearest_outside(
    finest: PyramidLevel, name: str, collected: BucketStats, boundary: int, step: int
) -> BucketStats:
    """격자 바깥 이웃 (0개 또는 1개). 경계 버킷에서 모은 것이 있으면 그것이 가장 가깝다."""
    if collected.count[0] > 0 or collected.has_gap[0]:
        return collected
    found = nearest_filled_bucket(finest, boundary, step)
    if found is None:
        return BucketStats.empty(0)
    return finest.stats(name, slice(found, found + 1))
