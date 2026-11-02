"""인터랙션 질의: 크로스헤어(최근접 원본 점)와 구간 통계 (계획 문서 3.2, P4).

값은 집계가 아니라 원본에서 나온다.
- 크로스헤어: 시리즈마다 x가 가장 가까운 **원본 행**의 값
- 구간 통계: [start, end) 원본 행 전체의 count/min/max/mean/first/last (정확)

구간 통계는 넓은 구간도 빠르고 정확하게 구한다. 구간 안에 완전히 들어가는 피라미드 버킷은
피라미드에서 merge하고, 양 끝의 버킷 한 칸보다 좁은 자투리만 원본에서 읽는다
(BucketStats 모노이드라 합쳐도 정확하다).
"""

import math
from dataclasses import dataclass

import numpy as np

from webchart.application.windows import SOURCE_RAW, usable_pyramid
from webchart.domain.bucket_stats import BucketStats
from webchart.domain.geometry import BucketGrid, XRange
from webchart.models.dataset import DatasetMeta
from webchart.services.data.access import raw_store
from webchart.services.data.x_domain import format_x


@dataclass(frozen=True, slots=True)
class CrosshairPoint:
    series: str
    x: float  # 정규 좌표
    y: float
    label: str  # 사람이 읽는 x (시간축은 표시 시간대의 ISO 문자열)


def nearest_points(
    meta: DatasetMeta,
    x_column: str,
    series: list[str],
    hover_x: float,
    search_width: float,
    timezone: str | None = None,
) -> list[CrosshairPoint]:
    """시리즈마다 hover_x에 가장 가까운 유효(결측 아닌) 원본 점.

    [hover_x - search_width, hover_x + search_width) 안과 그 양쪽 이웃 행에서 찾는다.
    범위 안에 점이 없어도 이웃 행이 있으므로, 데이터가 있으면 항상 무언가를 찾는다.
    """
    store = raw_store(meta)
    window = XRange(hover_x - search_width, hover_x + search_width)
    rows = store.read_range(x_column, window, series, include_neighbors=True)
    x_info = next((c for c in meta.columns if c.name == x_column), None)
    if timezone and x_info is not None:
        x_info = x_info.model_copy(update={"timezone": timezone})

    points = []
    for name in series:
        y = rows.table.column(name).to_numpy(zero_copy_only=False).astype(np.float64)
        valid = np.flatnonzero(~np.isnan(y))
        if len(valid) == 0:
            continue
        nearest = int(valid[np.argmin(np.abs(rows.x[valid] - hover_x))])
        x = float(rows.x[nearest])
        points.append(CrosshairPoint(name, x, float(y[nearest]), format_x(x, x_info)))
    return points


@dataclass(frozen=True, slots=True)
class SeriesSummary:
    count: int
    min: float | None
    max: float | None
    mean: float | None
    first: float | None
    last: float | None
    min_x: float | None
    max_x: float | None


@dataclass(frozen=True, slots=True)
class RangeSummary:
    series: dict[str, SeriesSummary]
    is_exact: bool
    source: str


def range_summary(
    meta: DatasetMeta, x_column: str, series: list[str], x_range: XRange
) -> RangeSummary:
    """구간 [start, end)의 시리즈별 정확한 통계."""
    reader = usable_pyramid(meta, x_column, series)
    if reader is None:
        return _summaries(_raw_stats(meta, x_column, series, x_range), SOURCE_RAW)

    level = reader.level(reader.info.max_level)
    grid = level.grid
    # 구간 안에 완전히 들어가는 버킷 [first_full, stop_full)
    first_full = max(0, math.ceil((x_range.start - grid.origin) / grid.bucket_width))
    stop_full = min(grid.bucket_count, math.floor((x_range.end - grid.origin) / grid.bucket_width))
    if stop_full <= first_full:
        return _summaries(_raw_stats(meta, x_column, series, x_range), SOURCE_RAW)

    inner_start = grid.origin + first_full * grid.bucket_width
    inner_end = grid.origin + stop_full * grid.bucket_width
    combined: dict[str, BucketStats] = {}
    left = _raw_stats(meta, x_column, series, _maybe_range(x_range.start, inner_start))
    right = _raw_stats(meta, x_column, series, _maybe_range(inner_end, x_range.end))
    for name in series:
        inner = level.stats(name, slice(first_full, stop_full)).regroup(np.array([0]))
        combined[name] = left[name].merge(inner).merge(right[name])
    return _summaries(combined, f"pyramid:L{level.level}+raw")


def _maybe_range(start: float, end: float) -> XRange | None:
    return XRange(start, end) if end > start else None


def _raw_stats(
    meta: DatasetMeta, x_column: str, series: list[str], x_range: XRange | None
) -> dict[str, BucketStats]:
    if x_range is None:
        return {name: BucketStats.empty(1) for name in series}
    windows = raw_store(meta).aggregate(x_column, series, BucketGrid.covering(x_range, 1))
    return {name: window.stats for name, window in windows.items()}


def _summaries(stats: dict[str, BucketStats], source: str) -> RangeSummary:
    return RangeSummary({name: _summary(one) for name, one in stats.items()}, True, source)


def _summary(stats: BucketStats) -> SeriesSummary:
    count = int(stats.count[0])
    if count == 0:
        return SeriesSummary(0, None, None, None, None, None, None, None)
    return SeriesSummary(
        count=count,
        min=float(stats.min_y[0]),
        max=float(stats.max_y[0]),
        mean=float(stats.sum_y[0] / count),
        first=float(stats.first_y[0]),
        last=float(stats.last_y[0]),
        min_x=float(stats.min_x[0]),
        max_x=float(stats.max_x[0]),
    )
