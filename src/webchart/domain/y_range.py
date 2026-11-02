"""y범위 값 객체와 정책 (계획 문서 2.5, D1 해소).

타일을 이어 붙였을 때 선이 맞으려면 한 화면의 모든 타일이 같은 y범위를 써야 한다.
그래서 y범위는 타일마다 자동으로 정하지 않고, 프레임 단위로 한 번 정해 모든 타일에
넘긴다. y범위는 타일 캐시 키에도 들어간다.

정책
- GLOBAL: 데이터셋 전체 y 최솟값·최댓값에서 계산한다. 줌·이동에도 변하지 않아 캐시 효율이 가장 좋다.
- VIEWPORT: 보이는 x구간의 y 최솟값·최댓값에서 계산하고, 캐시 적중을 위해 보기 좋은 값으로 넓힌다.
- FIXED: 사용자가 지정한 범위를 그대로 쓴다.

자동 범위는 matplotlib의 선형 축 자동 스케일과 같은 규칙(여백 5%, 값이 하나면 넓히기)을 쓴다.
원본 전체를 matplotlib로 그린 그림(오라클 정답)과 y축이 같아야 하기 때문이다.
"""

import math
from dataclasses import dataclass
from enum import Enum

# matplotlib axes.ymargin 기본값
AUTOSCALE_MARGIN = 0.05

# matplotlib.transforms.nonsingular의 기본 인자 (MaxNLocator.nonsingular가 쓰는 값)
_NONSINGULAR_EXPANDER = 0.05
_NONSINGULAR_TINY = 1e-15


class YRangePolicy(str, Enum):
    GLOBAL = "global"
    VIEWPORT = "viewport"
    FIXED = "fixed"


@dataclass(frozen=True, slots=True)
class YRange:
    """y축 표시 범위 `[low, high]` (그림의 아래 끝과 위 끝)."""

    low: float
    high: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.low) and math.isfinite(self.high)):
            raise ValueError(f"y범위는 유한해야 한다: {self.low}..{self.high}")
        if self.high <= self.low:
            raise ValueError(f"y범위가 비었다: {self.low}..{self.high}")

    def as_tuple(self) -> tuple[float, float]:
        return (self.low, self.high)

    @classmethod
    def autoscaled(cls, data_low: float, data_high: float) -> "YRange":
        """데이터 최솟값·최댓값 → matplotlib 자동 스케일과 같은 표시 범위."""
        return cls(*autoscale_bounds(data_low, data_high))

    def snapped_outward(self, steps: int = 8) -> "YRange":
        """눈금 간격(1·2·5 × 10^k)의 배수로 바깥쪽으로 넓힌다.

        VIEWPORT 정책에서 화면을 조금 이동해도 같은 y범위가 나오게 해 타일 캐시를
        다시 쓰기 위한 것이다. 범위는 넓어지기만 하므로 데이터가 잘리지 않는다.
        """
        step = nice_step((self.high - self.low) / steps)
        return YRange(math.floor(self.low / step) * step, math.ceil(self.high / step) * step)


def autoscale_bounds(data_low: float, data_high: float) -> tuple[float, float]:
    """matplotlib 선형 축 자동 스케일 규칙: 폭 0 범위 넓히기 → 양쪽 여백 5%.

    y축뿐 아니라 x 범위를 지정하지 않은 정적 렌더의 x축에도 같은 규칙을 쓴다.
    """
    low, high = _nonsingular(data_low, data_high)
    margin = (high - low) * AUTOSCALE_MARGIN
    return low - margin, high + margin


def nice_step(raw_step: float) -> float:
    """raw_step 이상인 가장 작은 1·2·5 × 10^k 값."""
    if raw_step <= 0 or not math.isfinite(raw_step):
        raise ValueError(f"간격은 양수여야 한다: {raw_step}")
    magnitude = 10.0 ** math.floor(math.log10(raw_step))
    for multiplier in (1.0, 2.0, 5.0, 10.0):
        if raw_step <= multiplier * magnitude:
            return multiplier * magnitude
    return 10.0 * magnitude  # 부동소수 오차로 위에서 걸리지 않은 경우


def _nonsingular(low: float, high: float) -> tuple[float, float]:
    """matplotlib.transforms.nonsingular와 같은 규칙으로 폭이 0인 범위를 넓힌다."""
    if not (math.isfinite(low) and math.isfinite(high)):
        return -_NONSINGULAR_EXPANDER, _NONSINGULAR_EXPANDER
    if high < low:
        low, high = high, low
    largest = max(abs(low), abs(high))
    if largest < (1e6 / _NONSINGULAR_TINY) * 2.2250738585072014e-308:  # float 최소 정규수
        return -_NONSINGULAR_EXPANDER, _NONSINGULAR_EXPANDER
    if high - low <= largest * _NONSINGULAR_TINY:
        if high == 0 and low == 0:
            return -_NONSINGULAR_EXPANDER, _NONSINGULAR_EXPANDER
        return low - _NONSINGULAR_EXPANDER * abs(low), high + _NONSINGULAR_EXPANDER * abs(high)
    return low, high
