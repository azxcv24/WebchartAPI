"""투영: 버킷 집계 → 그릴 도형 (계획 문서 2.3 표).

투영은 같은 BucketStats에서 차트 종류마다 필요한 모양을 만든다.
- line: M4 (픽셀 열마다 first, min, max, last 네 점)
- band: 픽셀 열마다 min~max 띠

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
    return Polyline(x, y)


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
