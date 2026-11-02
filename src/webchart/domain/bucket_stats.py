"""BucketStats: 버킷별 집계 모노이드 (계획 문서 2.3).

버킷마다 한 시리즈의 다음 값을 보관한다.

    count, sum_y                       유효 행(y가 결측이 아닌 행) 수와 y 합
    first_x, first_y / last_x, last_y  x가 가장 작은 / 가장 큰 유효 점
    min_x, min_y / max_x, max_y        y가 가장 작은 / 가장 큰 유효 점 (그 점의 x까지 보관)
    gap_first_x, gap_last_x            y가 결측인 행의 x 최소·최대 (선 끊기 판정용)

이 값들은 버킷 두 개를 합쳐도(merge) 정확하게 다시 계산된다. 그래서
- 세밀한 레벨을 2개씩 합쳐 상위 레벨을 정확히 만들고 (`merge_pairs`)
- 피라미드 버킷을 픽셀 열로 다시 묶고 (`regroup`)
- 데이터를 나눠 읽어 집계한 뒤 합칠 수 있다 (`merge`).

저장 구조는 버킷 하나당 객체가 아니라 필드마다 numpy 배열 하나다 (struct of arrays).
버킷 수천만 개를 파이썬 루프 없이 벡터 연산으로 다루기 위해서다.

빈 버킷의 값: count=0, sum_y=0, first_x=+inf, last_x=-inf, min_y=+inf, max_y=-inf,
나머지 좌표는 NaN, gap_first_x=+inf, gap_last_x=-inf. 이렇게 두면 merge가
"작은 쪽/큰 쪽 고르기"만으로 빈 버킷을 자연스럽게 무시한다.
"""

from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Self

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

# reduceat에서 "해당 없음"을 나타내는 인덱스 값
_NO_INDEX = np.iinfo(np.int64).max


@dataclass(frozen=True, slots=True)
class BucketStats:
    """버킷 n개의 집계. 모든 필드는 길이 n인 배열이고 i번째 원소가 버킷 i다."""

    count: IntArray
    sum_y: FloatArray
    first_x: FloatArray
    first_y: FloatArray
    last_x: FloatArray
    last_y: FloatArray
    min_x: FloatArray
    min_y: FloatArray
    max_x: FloatArray
    max_y: FloatArray
    gap_first_x: FloatArray
    gap_last_x: FloatArray

    # --- 생성 ---------------------------------------------------------------

    @classmethod
    def empty(cls, size: int) -> Self:
        """빈 버킷 `size`개."""
        nan = np.full(size, np.nan)
        return cls(
            count=np.zeros(size, dtype=np.int64),
            sum_y=np.zeros(size),
            first_x=np.full(size, np.inf),
            first_y=nan.copy(),
            last_x=np.full(size, -np.inf),
            last_y=nan.copy(),
            min_x=nan.copy(),
            min_y=np.full(size, np.inf),
            max_x=nan.copy(),
            max_y=np.full(size, -np.inf),
            gap_first_x=np.full(size, np.inf),
            gap_last_x=np.full(size, -np.inf),
        )

    @classmethod
    def from_points(cls, x: FloatArray, y: FloatArray) -> Self:
        """점 하나를 버킷 하나로 보는 집계. y가 NaN인 점은 결측(gap)으로 기록한다.

        x는 결측이 없어야 한다 (위치를 모르는 점은 그릴 수 없으므로 호출자가 먼저 버린다).
        """
        is_valid = ~np.isnan(y)
        valid_x = np.where(is_valid, x, np.nan)
        valid_y = np.where(is_valid, y, np.nan)
        gap_x = np.where(is_valid, np.nan, x)
        return cls(
            count=is_valid.astype(np.int64),
            sum_y=np.where(is_valid, y, 0.0),
            first_x=np.where(is_valid, x, np.inf),
            first_y=valid_y,
            last_x=np.where(is_valid, x, -np.inf),
            last_y=valid_y.copy(),
            min_x=valid_x,
            min_y=np.where(is_valid, y, np.inf),
            max_x=valid_x.copy(),
            max_y=np.where(is_valid, y, -np.inf),
            gap_first_x=np.where(is_valid, np.inf, gap_x),
            gap_last_x=np.where(is_valid, -np.inf, gap_x),
        )

    @classmethod
    def concat(cls, parts: Sequence["BucketStats"]) -> Self:
        """버킷 열들을 순서대로 이어 붙인다."""
        return cls(
            **{
                name: np.concatenate([getattr(part, name) for part in parts])
                for name in _FIELD_NAMES
            }
        )

    # --- 조회 ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.count)

    @property
    def has_values(self) -> npt.NDArray[np.bool_]:
        """유효 점이 하나 이상 있는 버킷."""
        return self.count > 0

    @property
    def has_gap(self) -> npt.NDArray[np.bool_]:
        """y 결측 행이 하나 이상 있는 버킷."""
        return np.isfinite(self.gap_first_x)

    @property
    def mean_y(self) -> FloatArray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(self.count > 0, self.sum_y / self.count, np.nan)

    def take(self, indices: IntArray | slice) -> "BucketStats":
        """일부 버킷만 고른다 (slice면 numpy 뷰라 복사하지 않는다)."""
        return BucketStats(**{name: getattr(self, name)[indices] for name in _FIELD_NAMES})

    def y_bounds(self) -> tuple[float, float] | None:
        """모든 버킷의 y 최솟값·최댓값. 유효 점이 없으면 None."""
        if not self.has_values.any():
            return None
        return float(self.min_y.min()), float(self.max_y.max())

    # --- 합치기 -------------------------------------------------------------

    def merge(self, later: "BucketStats") -> "BucketStats":
        """같은 위치의 버킷끼리 합친다. `later`는 x 순서상 self와 같거나 뒤쪽 데이터다.

        first/last를 x 비교로 고르므로, 순서가 섞인 데이터를 합쳐도 결과가 같다.
        x가 같은 점끼리는 first는 self를, last는 later를 고른다 (입력 순서 유지).
        min/max의 값이 같으면 self(먼저 나온 점)를 유지한다.
        """
        take_first = later.first_x < self.first_x
        take_last = later.last_x >= self.last_x
        take_min = later.min_y < self.min_y
        take_max = later.max_y > self.max_y
        return BucketStats(
            count=self.count + later.count,
            sum_y=self.sum_y + later.sum_y,
            first_x=np.where(take_first, later.first_x, self.first_x),
            first_y=np.where(take_first, later.first_y, self.first_y),
            last_x=np.where(take_last, later.last_x, self.last_x),
            last_y=np.where(take_last, later.last_y, self.last_y),
            min_x=np.where(take_min, later.min_x, self.min_x),
            min_y=np.where(take_min, later.min_y, self.min_y),
            max_x=np.where(take_max, later.max_x, self.max_x),
            max_y=np.where(take_max, later.max_y, self.max_y),
            gap_first_x=np.minimum(self.gap_first_x, later.gap_first_x),
            gap_last_x=np.maximum(self.gap_last_x, later.gap_last_x),
        )

    def regroup(self, group_starts: IntArray) -> "BucketStats":
        """연속한 버킷들을 묶어 하나로 합친다 (`np.ufunc.reduceat`과 같은 규칙).

        그룹 g는 버킷 `[group_starts[g], group_starts[g+1])` 이고, 마지막 그룹은 끝까지다.
        group_starts는 오름차순이고 서로 달라야 한다 (빈 그룹 금지).
        버킷이 x 순서로 놓여 있다고 가정한다 (피라미드 레벨, 정렬된 점 열).
        """
        starts = np.asarray(group_starts, dtype=np.int64)
        positions = np.arange(len(self), dtype=np.int64)
        has_values = self.has_values

        first_at = np.minimum.reduceat(np.where(has_values, positions, _NO_INDEX), starts)
        last_at = np.maximum.reduceat(np.where(has_values, positions, -1), starts)
        min_y = np.minimum.reduceat(self.min_y, starts)
        max_y = np.maximum.reduceat(self.max_y, starts)
        # 그룹 최솟값과 같은 첫 버킷 = 그 최솟값이 처음 나온 곳
        lengths = np.diff(np.append(starts, len(self)))
        is_min = has_values & (self.min_y == np.repeat(min_y, lengths))
        is_max = has_values & (self.max_y == np.repeat(max_y, lengths))
        min_at = np.minimum.reduceat(np.where(is_min, positions, _NO_INDEX), starts)
        max_at = np.minimum.reduceat(np.where(is_max, positions, _NO_INDEX), starts)

        return BucketStats(
            count=np.add.reduceat(self.count, starts),
            sum_y=np.add.reduceat(self.sum_y, starts),
            first_x=_gather(self.first_x, first_at, np.inf),
            first_y=_gather(self.first_y, first_at, np.nan),
            last_x=_gather(self.last_x, last_at, -np.inf),
            last_y=_gather(self.last_y, last_at, np.nan),
            min_x=_gather(self.min_x, min_at, np.nan),
            min_y=min_y,
            max_x=_gather(self.max_x, max_at, np.nan),
            max_y=max_y,
            gap_first_x=np.minimum.reduceat(self.gap_first_x, starts),
            gap_last_x=np.maximum.reduceat(self.gap_last_x, starts),
        )

    def merge_pairs(self) -> "BucketStats":
        """버킷 (2k, 2k+1)을 합쳐 버킷 k로 만든다. 피라미드 레벨 L → L-1."""
        if len(self) % 2:
            raise ValueError(f"버킷 수가 짝수여야 한다: {len(self)}")
        return self.regroup(np.arange(0, len(self), 2, dtype=np.int64))


_FIELD_NAMES = tuple(field.name for field in fields(BucketStats))


def _gather(values: FloatArray, at: IntArray, missing: float) -> FloatArray:
    """`values[at]`. at이 해당 없음(_NO_INDEX 또는 -1)이면 `missing`."""
    is_found = (at >= 0) & (at < len(values))
    safe_at = np.where(is_found, at, 0)
    gathered = values[safe_at] if len(values) else np.full(len(at), missing)
    return np.where(is_found, gathered, missing)


def aggregate_sorted(
    x: FloatArray, y: FloatArray, bucket_index: IntArray
) -> tuple[IntArray, BucketStats]:
    """x 오름차순 점들을 버킷별로 집계한다. O(n).

    Args:
        x: 점의 x (오름차순, 결측 없음)
        y: 점의 y (결측은 NaN, gap으로 기록)
        bucket_index: 각 점의 버킷 번호 (x가 오름차순이므로 이것도 오름차순)

    Returns:
        (점이 하나라도 있는 버킷 번호들, 그 버킷들의 집계)
    """
    if len(x) == 0:
        return np.empty(0, dtype=np.int64), BucketStats.empty(0)
    is_new_bucket = np.empty(len(bucket_index), dtype=bool)
    is_new_bucket[0] = True
    np.not_equal(bucket_index[1:], bucket_index[:-1], out=is_new_bucket[1:])
    starts = np.flatnonzero(is_new_bucket)
    return bucket_index[starts], BucketStats.from_points(x, y).regroup(starts)


def merge_into(target: BucketStats, at: IntArray, part: BucketStats) -> None:
    """`target`의 버킷 `at`에 `part`를 합친다 (target 배열을 직접 고친다).

    피라미드를 만들 때 큰 누적 배열을 매번 복사하지 않기 위한 함수다.
    at은 서로 달라야 한다. part는 target[at]보다 x 순서상 뒤쪽 데이터로 간주한다.
    """
    merged = target.take(at).merge(part)
    for name in _FIELD_NAMES:
        getattr(target, name)[at] = getattr(merged, name)
