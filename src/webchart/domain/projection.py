"""투영: 버킷 집계 → 그릴 도형 (계획 문서 2.3 표).

투영은 같은 BucketStats에서 차트 종류마다 필요한 모양을 만든다.
- line / area: M4 (픽셀 열마다 first, min, max, last 네 점)
- band: 픽셀 열마다 min~max 띠
- bar: 열 묶음마다 집계값(sum/mean/count/min/max) 막대
- candlestick: 열 묶음마다 OHLC (first, max, min, last)
- scatter / heatmap: BucketStats가 아니라 2D 밀도 격자(픽셀별 점 개수)

막대·캔들은 픽셀 열을 2의 거듭제곱 개씩 묶는다. 타일 폭이 묶음 크기로 나눠떨어져야
막대가 타일 경계에서 잘리지 않고, 묶음 경계도 피라미드 버킷 경계와 맞아 값이 정확하다.

결측 처리(P5): y 결측 행이 두 유효 점 사이에 있으면 그 사이 선을 끊는다 (NaN 구분자).
matplotlib도 원본에 NaN이 있으면 같은 자리에서 선을 끊으므로 원본 그림과 같다.
한 픽셀 열 안에서만 생긴 결측(버킷 내부 결측)은 그 열의 세로 범위 안에 묻히므로 끊지 않는다.
"""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from webchart.domain.bucket_stats import BucketStats, FloatArray, IntArray
from webchart.domain.geometry import BucketGrid


@dataclass(frozen=True, slots=True)
class SeriesWindow:
    """격자 하나에 대한 한 시리즈의 집계와 좌우 이웃.

    Attributes:
        grid: 집계 격자 (그림의 픽셀 열과 같다)
        stats: 격자 버킷별 집계 (길이 = grid.bucket_count)
        lead: 격자 왼쪽 바깥에서 데이터가 있는 가장 가까운 버킷 (0개 또는 1개).
            선이 그림의 왼쪽 끝까지 이어지게 하는 데 쓴다.
        trail: 격자 오른쪽 바깥의 같은 역할 버킷 (0개 또는 1개)
    """

    grid: BucketGrid
    stats: BucketStats
    lead: BucketStats
    trail: BucketStats

    def __post_init__(self) -> None:
        if len(self.stats) != self.grid.bucket_count:
            raise ValueError(f"집계 길이 {len(self.stats)} ≠ 버킷 수 {self.grid.bucket_count}")
        if len(self.lead) > 1 or len(self.trail) > 1:
            raise ValueError("lead/trail은 버킷 0개 또는 1개여야 한다")

    def with_neighbors(self) -> BucketStats:
        """lead + 격자 버킷 + trail 을 x 순서로 이어 붙인 집계."""
        return BucketStats.concat([self.lead, self.stats, self.trail])


@dataclass(frozen=True, slots=True)
class Polyline:
    """그릴 꺾은선. NaN인 점에서 선이 끊긴다."""

    x: FloatArray
    y: FloatArray

    def __len__(self) -> int:
        return len(self.x)

    @property
    def point_count(self) -> int:
        """구분자(NaN)를 뺀 실제 점 수."""
        return int(np.count_nonzero(~np.isnan(self.y)))


def m4_polyline(window: SeriesWindow) -> Polyline:
    """M4 투영: 버킷마다 first·min·max·last 네 점을 x 순서로 잇는다.

    M4 정리(Jugel et al., PVLDB 2014): 버킷 경계 = 픽셀 경계이면 이 꺾은선의 래스터는
    원본 전체 꺾은선의 래스터와 같다. 각 픽셀 열의 세로 범위가 min~max로 같고,
    열과 열을 잇는 선분(이전 열의 last → 다음 열의 first)도 원본과 같기 때문이다.
    """
    stats = window.with_neighbors()
    filled = np.flatnonzero(stats.has_values)
    if len(filled) == 0:
        return Polyline(np.empty(0), np.empty(0))

    picked = stats.take(filled)
    min_comes_first = picked.min_x <= picked.max_x
    middle_x = np.where(min_comes_first, picked.min_x, picked.max_x)
    middle_y = np.where(min_comes_first, picked.min_y, picked.max_y)
    later_x = np.where(min_comes_first, picked.max_x, picked.min_x)
    later_y = np.where(min_comes_first, picked.max_y, picked.min_y)

    breaks_after = _breaks_between(stats, filled)
    nan_column = np.full(len(filled), np.nan)
    columns_x = (picked.first_x, middle_x, later_x, picked.last_x, nan_column)
    columns_y = (picked.first_y, middle_y, later_y, picked.last_y, nan_column)
    # 다섯째 칸(NaN 구분자)은 선을 끊어야 하는 곳에만 남긴다
    keep = np.ones((len(filled), 5), dtype=bool)
    keep[:, 4] = breaks_after
    x = np.column_stack(columns_x)[keep]
    y = np.column_stack(columns_y)[keep]
    # 점이 하나뿐인 버킷은 first=min=max=last라 같은 점이 4번 나온다. 연속 중복은 버린다
    # (같은 점을 다시 그려도 모양은 같으므로 그림에 영향이 없다).
    is_repeat = np.zeros(len(x), dtype=bool)
    is_repeat[1:] = (x[1:] == x[:-1]) & (y[1:] == y[:-1])
    return Polyline(x[~is_repeat], y[~is_repeat])


def _breaks_between(stats: BucketStats, filled: IntArray) -> npt.NDArray[np.bool_]:
    """유효 버킷 filled[i]와 filled[i+1] 사이에서 선을 끊어야 하는지 (길이 = len(filled)).

    마지막 원소는 뒤에 이을 버킷이 없으므로 의미가 없다 (항상 False).
    두 유효 점 사이에 결측 행이 있으면 끊는다.
    - 앞 버킷의 마지막 결측이 그 버킷의 last 뒤에 있음
    - 뒤 버킷의 첫 결측이 그 버킷의 first 앞에 있음
    - 사이에 낀 버킷(유효 점 없음)에 결측이 있음
    """
    breaks = np.zeros(len(filled), dtype=bool)
    if len(filled) < 2:
        return breaks
    before, after = filled[:-1], filled[1:]
    trailing_gap = stats.gap_last_x[before] > stats.last_x[before]
    leading_gap = stats.gap_first_x[after] < stats.first_x[after]
    gap_prefix = np.concatenate([[0], np.cumsum(stats.has_gap)])
    gap_between = gap_prefix[after] - gap_prefix[before + 1] > 0
    breaks[:-1] = trailing_gap | leading_gap | gap_between
    return breaks


# --- 구간(열 묶음) 투영: band, bar, candlestick ------------------------------------


@dataclass(frozen=True, slots=True)
class Spans:
    """x 구간마다 값 하나 이상을 가진 도형의 공통 형태 (값이 있는 구간만 담는다).

    left_x/right_x는 구간 경계(격자 경계와 같다)이고, 나머지 배열은 구간별 값이다.
    """

    left_x: FloatArray
    right_x: FloatArray

    def __len__(self) -> int:
        return len(self.left_x)


@dataclass(frozen=True, slots=True)
class Band(Spans):
    """픽셀 열마다 [low, high] 띠. 원본의 열별 최솟값·최댓값과 같다 (정확)."""

    low: FloatArray
    high: FloatArray

    def y_bounds(self) -> tuple[float, float] | None:
        return _bounds(self.low, self.high)


@dataclass(frozen=True, slots=True)
class Bars(Spans):
    """구간마다 막대 하나. value는 구간 원본 행의 집계값이다 (정확)."""

    value: FloatArray

    def y_bounds(self) -> tuple[float, float] | None:
        # 막대는 0에서 시작하므로 0을 범위에 넣는다
        return _bounds(np.append(self.value, 0.0), np.append(self.value, 0.0))


@dataclass(frozen=True, slots=True)
class Candles(Spans):
    """구간마다 OHLC 캔들. open=first, high=max, low=min, close=last (정확)."""

    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray

    def y_bounds(self) -> tuple[float, float] | None:
        return _bounds(self.low, self.high)


def _bounds(low: FloatArray, high: FloatArray) -> tuple[float, float] | None:
    if len(low) == 0:
        return None
    return float(np.nanmin(low)), float(np.nanmax(high))


def _grouped(window: SeriesWindow, columns_per_group: int) -> tuple[BucketStats, FloatArray]:
    """격자 버킷을 columns_per_group개씩 묶은 집계와 각 묶음의 왼쪽 경계."""
    grid = window.grid
    if grid.bucket_count % columns_per_group:
        raise ValueError(
            f"버킷 수 {grid.bucket_count}가 묶음 크기 {columns_per_group}로 나눠떨어지지 않는다"
        )
    starts = np.arange(0, grid.bucket_count, columns_per_group, dtype=np.int64)
    left_x = grid.origin + starts * grid.bucket_width
    return window.stats.regroup(starts), left_x


def envelope_band(window: SeriesWindow) -> Band:
    """band 투영: 값이 있는 픽셀 열마다 [min, max]."""
    grid = window.grid
    filled = np.flatnonzero(window.stats.has_values)
    left_x = grid.origin + filled * grid.bucket_width
    return Band(
        left_x=left_x,
        right_x=left_x + grid.bucket_width,
        low=window.stats.min_y[filled],
        high=window.stats.max_y[filled],
    )


# BarAggregate 값(문자열) → BucketStats에서 막대 값을 꺼내는 방법
_BAR_VALUES = {
    "sum": lambda stats: stats.sum_y,
    "mean": lambda stats: stats.mean_y,
    "count": lambda stats: stats.count.astype(np.float64),
    "min": lambda stats: stats.min_y,
    "max": lambda stats: stats.max_y,
}


def aggregate_bars(window: SeriesWindow, aggregate: str, columns_per_bar: int) -> Bars:
    """bar 투영: columns_per_bar개 픽셀 열마다 막대 하나, 값은 그 x 구간 원본의 집계."""
    if aggregate not in _BAR_VALUES:
        raise ValueError(f"알 수 없는 막대 집계: {aggregate}")
    grouped, left_x = _grouped(window, columns_per_bar)
    filled = np.flatnonzero(grouped.has_values)
    width = window.grid.bucket_width * columns_per_bar
    return Bars(
        left_x=left_x[filled],
        right_x=left_x[filled] + width,
        value=_BAR_VALUES[aggregate](grouped)[filled],
    )


def price_candles(window: SeriesWindow, columns_per_candle: int) -> Candles:
    """candlestick 투영 (원시 가격 한 컬럼): 구간의 first/max/min/last."""
    grouped, left_x = _grouped(window, columns_per_candle)
    filled = np.flatnonzero(grouped.has_values)
    width = window.grid.bucket_width * columns_per_candle
    return Candles(
        left_x=left_x[filled],
        right_x=left_x[filled] + width,
        open=grouped.first_y[filled],
        high=grouped.max_y[filled],
        low=grouped.min_y[filled],
        close=grouped.last_y[filled],
    )


def ohlc_candles(
    open_: SeriesWindow,
    high: SeriesWindow,
    low: SeriesWindow,
    close: SeriesWindow,
    columns_per_candle: int,
) -> Candles:
    """candlestick 투영 (이미 만든 OHLC 컬럼): first(open), max(high), min(low), last(close)."""
    open_stats, left_x = _grouped(open_, columns_per_candle)
    high_stats, _ = _grouped(high, columns_per_candle)
    low_stats, _ = _grouped(low, columns_per_candle)
    close_stats, _ = _grouped(close, columns_per_candle)
    filled = np.flatnonzero(
        open_stats.has_values
        & high_stats.has_values
        & low_stats.has_values
        & close_stats.has_values
    )
    width = open_.grid.bucket_width * columns_per_candle
    return Candles(
        left_x=left_x[filled],
        right_x=left_x[filled] + width,
        open=open_stats.first_y[filled],
        high=high_stats.max_y[filled],
        low=low_stats.min_y[filled],
        close=close_stats.last_y[filled],
    )


# --- 2D 밀도 (scatter, heatmap) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DensityGrid:
    """픽셀(열 × 행)별 점 개수. counts[행, 열], 행 0이 y 아래쪽이다.

    정확성: 개수는 원본 점을 같은 픽셀 경계로 센 값과 같다 (계획 문서 8장 density).
    """

    counts: npt.NDArray[np.int64]
    grid: BucketGrid
    y_low: float
    y_high: float

    @property
    def rows(self) -> int:
        return int(self.counts.shape[0])

    def row_index_of(self, y: FloatArray) -> npt.NDArray[np.int64]:
        """각 y가 속한 행. 범위 밖이면 음수이거나 rows 이상이다."""
        height = (self.y_high - self.y_low) / self.rows
        index: npt.NDArray[np.int64] = np.floor((y - self.y_low) / height).astype(np.int64)
        return index


def empty_density(grid: BucketGrid, y_low: float, y_high: float, rows: int) -> DensityGrid:
    return DensityGrid(np.zeros((rows, grid.bucket_count), dtype=np.int64), grid, y_low, y_high)


def add_points(density: DensityGrid, x: FloatArray, y: FloatArray) -> None:
    """점들을 밀도 격자에 더한다 (counts를 직접 고친다). 격자 밖·결측 점은 버린다.

    x는 반열린 구간 규칙, y는 위쪽 끝(y_high)을 포함한다 (y범위 위 끝에 딱 맞는 최댓값이
    빠지지 않게).
    """
    finite = np.isfinite(x) & np.isfinite(y)
    x, y = x[finite], y[finite]
    column = density.grid.index_of(x)
    row = density.row_index_of(y)
    row = np.where(y == density.y_high, density.rows - 1, row)
    inside = (
        (column >= 0) & (column < density.grid.bucket_count) & (row >= 0) & (row < density.rows)
    )
    flat = row[inside] * density.grid.bucket_count + column[inside]
    # 칠해진 셀에만 더한다. bincount(minlength=전체 셀 수)는 점이 적어도 격자 전체
    # (피라미드 빌드 때 수백만 셀)를 매번 새로 만들고 더해 매우 느리다.
    cells, counts = np.unique(flat, return_counts=True)
    density.counts.reshape(-1)[cells] += counts


def normalize_density(counts: npt.NDArray[np.int64], how: str) -> FloatArray:
    """개수 → 농도 [0, 1]. 개수 0인 픽셀은 0(투명), 1개 이상은 0보다 크다.

    - linear: count / max
    - log: log1p(count) / log1p(max)
    - eq_hist: 0이 아닌 개수들의 순위(경험적 누적분포)로 펼친다
    """
    values = counts.astype(np.float64)
    occupied = counts > 0
    if not occupied.any():
        return np.zeros_like(values)
    if how == "linear":
        linear: FloatArray = values / values.max()
        return linear
    if how == "log":
        logged: FloatArray = np.log1p(values) / np.log1p(values.max())
        return logged
    if how == "eq_hist":
        distinct, inverse = np.unique(counts[occupied], return_inverse=True)
        ranks = (np.arange(1, len(distinct) + 1) / len(distinct))[inverse]
        result = np.zeros_like(values)
        result[occupied] = ranks
        return result
    raise ValueError(f"알 수 없는 밀도 정규화: {how}")
