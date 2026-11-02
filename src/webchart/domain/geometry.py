"""차트 좌표계 값 객체: x 구간, 타일 격자, 화면 변환.

x → 타일 → 픽셀 변환은 모두 이 모듈을 거친다. 피라미드 버킷, 타일, 호버,
축 눈금이 같은 격자(`PixelLattice`)를 공유해야 집계 버킷 경계와 픽셀 경계가
일치하고, 그래야 M4 집계가 원본과 같은 그림을 만든다 (계획 문서 2.2).

단위 규칙
- x, y: 데이터 좌표. 시간축은 epoch 나노초로 정규화한 float64 값이다.
- `*_px`: 픽셀.
- 구간은 반열린 구간 `[start, end)` 이다.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

# 타일 줌 상한. 2^24 타일이면 1년치 데이터의 타일 하나가 약 2초 폭이라 충분하다.
# ViewportWindow.zoom 검증 범위(le=24)와 같은 값을 쓴다.
MAX_ZOOM = 24

# 허용하는 장치 픽셀 비율(dpr). 2의 거듭제곱이어야 타일 버킷이 피라미드 레벨과 맞는다.
ALLOWED_DPR = (1, 2, 4)


def is_power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


@dataclass(frozen=True, slots=True)
class XRange:
    """x축의 반열린 구간 `[start, end)`.

    빈 구간이나 무한대 경계는 만들 수 없다. 이런 값이 들어오면 이후의 모든
    좌표 계산(0으로 나누기 등)이 조용히 틀어지므로 생성 시점에 거부한다.
    """

    start: float
    end: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.start) and math.isfinite(self.end)):
            raise ValueError(f"구간 경계는 유한해야 한다: [{self.start}, {self.end})")
        if self.end <= self.start:
            raise ValueError(f"빈 구간: [{self.start}, {self.end})")

    @property
    def width(self) -> float:
        return self.end - self.start

    def contains(self, x: float) -> bool:
        return self.start <= x < self.end

    def overlaps(self, other: "XRange") -> bool:
        return self.start < other.end and other.start < self.end

    def as_tuple(self) -> tuple[float, float]:
        return (self.start, self.end)


@dataclass(frozen=True, slots=True)
class BucketGrid:
    """x 구간을 같은 폭의 버킷 `bucket_count`개로 나눈 격자.

    버킷 j는 `[origin + j·bucket_width, origin + (j+1)·bucket_width)` 이다.
    그림을 그릴 때는 버킷 하나가 픽셀 열 하나다. M4 집계가 원본과 같은 그림을 내려면
    버킷 경계가 픽셀 경계와 일치해야 하므로, 집계와 그리기가 반드시 같은 격자를 쓴다.
    """

    origin: float
    bucket_width: float
    bucket_count: int

    def __post_init__(self) -> None:
        if not (math.isfinite(self.origin) and math.isfinite(self.bucket_width)):
            raise ValueError(f"격자 값은 유한해야 한다: {self.origin}, {self.bucket_width}")
        if self.bucket_width <= 0:
            raise ValueError(f"bucket_width는 양수여야 한다: {self.bucket_width}")
        if self.bucket_count <= 0:
            raise ValueError(f"bucket_count는 양수여야 한다: {self.bucket_count}")

    @classmethod
    def covering(cls, x_range: XRange, bucket_count: int) -> "BucketGrid":
        """`x_range`를 `bucket_count`등분한 격자 (화면 폭 = 버킷 수)."""
        return cls(x_range.start, x_range.width / bucket_count, bucket_count)

    @property
    def end(self) -> float:
        return self.origin + self.bucket_width * self.bucket_count

    @property
    def x_range(self) -> XRange:
        return XRange(self.origin, self.end)

    def index_of(self, x: npt.NDArray[np.float64]) -> npt.NDArray[np.int64]:
        """각 x가 속한 버킷 번호. 격자 밖이면 음수이거나 bucket_count 이상이다."""
        index: npt.NDArray[np.int64] = np.floor((x - self.origin) / self.bucket_width).astype(
            np.int64
        )
        return index

    def sub_grid(self, first: int, count: int) -> "BucketGrid":
        """버킷 `[first, first + count)`만 떼어 낸 격자 (경계는 그대로 유지)."""
        if first < 0 or count <= 0 or first + count > self.bucket_count:
            raise ValueError(f"격자 범위 초과: [{first}, {first + count}) / {self.bucket_count}")
        return BucketGrid(self.origin + first * self.bucket_width, self.bucket_width, count)

    def alignment_ratio(self, finer: "BucketGrid", rel_tol: float = 1e-9) -> int | None:
        """이 격자의 모든 경계가 `finer` 격자의 경계와 겹치면 버킷당 finer 버킷 수를 반환한다.

        겹치지 않으면 None. 피라미드 레벨을 픽셀 열로 다시 묶을 때 결과가 정확한지
        (exact) 판정하는 데 쓴다. 부동소수 오차를 감안해 상대 오차 `rel_tol`을 허용한다.
        """
        ratio = self.bucket_width / finer.bucket_width
        whole_ratio = round(ratio)
        if whole_ratio < 1 or not math.isclose(ratio, whole_ratio, rel_tol=rel_tol):
            return None
        offset = (self.origin - finer.origin) / finer.bucket_width
        if not math.isclose(offset, round(offset), rel_tol=0, abs_tol=rel_tol * max(1, offset)):
            return None
        return whole_ratio


@dataclass(frozen=True, slots=True, order=True)
class TileAddress:
    """줌 `zoom`에서 x 전체를 2^zoom 등분했을 때 `index`번째 타일."""

    zoom: int
    index: int

    def __post_init__(self) -> None:
        if not 0 <= self.zoom <= MAX_ZOOM:
            raise ValueError(f"zoom은 0..{MAX_ZOOM} 범위여야 한다: {self.zoom}")
        if not 0 <= self.index < 2**self.zoom:
            raise ValueError(f"zoom {self.zoom}에서 index 범위 초과: {self.index}")

    @property
    def tile_id(self) -> str:
        """WS 프로토콜 v1의 타일 식별자 `"{zoom}-{index}-0"`.

        끝의 0은 y 방향 타일 인덱스다. 타일은 x 방향으로만 나누므로 항상 0이다.
        """
        return f"{self.zoom}-{self.index}-0"


@dataclass(frozen=True, slots=True)
class PixelLattice:
    """x 전체 범위(`domain`)를 2의 거듭제곱으로 균등 분할하는 격자.

    줌 z의 타일은 domain을 2^z 등분한 구간 하나이고, 폭은 `tile_size_px` 픽셀이다.
    `tile_size_px`를 2의 거듭제곱으로 제한하는 이유: 피라미드 레벨
    L = z + log2(tile_size_px) 에서 버킷 1개가 정확히 픽셀 열 1개가 된다.
    """

    domain: XRange
    tile_size_px: int

    def __post_init__(self) -> None:
        if not is_power_of_two(self.tile_size_px):
            raise ValueError(f"tile_size_px는 2의 거듭제곱이어야 한다: {self.tile_size_px}")

    @classmethod
    def from_data_bounds(cls, x_min: float, x_max: float, tile_size_px: int) -> "PixelLattice":
        """데이터의 x 최솟값·최댓값(둘 다 포함)으로 격자를 만든다.

        domain은 반열린 구간이므로 그대로 쓰면 x_max인 마지막 점이 빠진다.
        끝을 x_max 바로 다음 float로 잡아 마지막 점도 마지막 타일에 포함시킨다.
        값이 하나뿐인 데이터(x_min == x_max)는 폭 1의 구간으로 넓힌다.
        """
        if x_max < x_min:
            raise ValueError(f"x_max < x_min: {x_max} < {x_min}")
        if x_max == x_min:
            return cls(XRange(x_min - 0.5, x_max + 0.5), tile_size_px)
        return cls(XRange(x_min, math.nextafter(x_max, math.inf)), tile_size_px)

    def tile_count(self, zoom: int) -> int:
        return 1 << zoom  # 2**zoom (int ** int는 mypy에서 Any로 추론되어 시프트를 쓴다)

    def tile_width(self, zoom: int) -> float:
        """줌 `zoom`에서 타일 하나가 덮는 x 폭 (데이터 단위)."""
        return self.domain.width / self.tile_count(zoom)

    def tile_range(self, tile: TileAddress) -> XRange:
        """타일이 덮는 x 구간.

        마지막 타일의 끝은 곱셈 결과가 아니라 domain.end를 그대로 쓴다.
        부동소수 오차로 마지막 점이 어느 타일에도 속하지 않는 일을 막기 위해서다.
        """
        width = self.tile_width(tile.zoom)
        start = self.domain.start + tile.index * width
        is_last = tile.index == self.tile_count(tile.zoom) - 1
        end = self.domain.end if is_last else start + width
        return XRange(start, end)

    def level_grid(self, level: int) -> BucketGrid:
        """피라미드 레벨 `level`의 격자: domain을 2^level 등분한다."""
        count = 1 << level
        return BucketGrid(self.domain.start, self.domain.width / count, count)

    def tile_level(self, zoom: int, dpr: int = 1) -> int:
        """타일 픽셀 열 1개 = 버킷 1개가 되는 레벨: L = zoom + log2(tile_size_px · dpr)."""
        if dpr not in ALLOWED_DPR:
            raise ValueError(f"dpr은 {ALLOWED_DPR} 중 하나여야 한다: {dpr}")
        return zoom + (self.tile_size_px * dpr).bit_length() - 1

    def tile_grid(self, tile: TileAddress, dpr: int = 1) -> BucketGrid:
        """타일의 픽셀 열 격자. 레벨 격자의 일부를 잘라 내므로 경계가 피라미드와 정확히 같다."""
        columns_px = self.tile_size_px * dpr
        level = self.level_grid(self.tile_level(tile.zoom, dpr))
        return level.sub_grid(tile.index * columns_px, columns_px)

    def tile_index_at(self, x: float, zoom: int) -> int:
        """x를 포함하는 타일 인덱스. domain 밖의 x는 가장 가까운 끝 타일로 붙인다."""
        raw_index = math.floor((x - self.domain.start) / self.tile_width(zoom))
        return min(max(raw_index, 0), self.tile_count(zoom) - 1)

    def tiles_covering(self, view: XRange, zoom: int) -> list[TileAddress]:
        """`view`와 겹치는 타일 목록 (인덱스 오름차순). 겹치는 타일이 없으면 빈 목록.

        view.end가 타일 경계에 정확히 걸리면 그 다음 타일은 포함하지 않는다
        (반열린 구간이므로 view에 그 타일의 점이 하나도 없다).
        """
        if not view.overlaps(self.domain):
            return []
        width = self.tile_width(zoom)
        first = self.tile_index_at(view.start, zoom)
        last_unclamped = math.ceil((view.end - self.domain.start) / width) - 1
        last = min(last_unclamped, self.tile_count(zoom) - 1)
        return [TileAddress(zoom, index) for index in range(first, last + 1)]

    def with_neighbors(self, tiles: Iterable[TileAddress], radius: int) -> list[TileAddress]:
        """각 타일의 좌우 `radius`개 이웃까지 더한 목록 (중복 제거, 오름차순).

        프리페치용이다. 격자 밖의 인덱스는 버린다.
        """
        expanded: set[TileAddress] = set()
        for tile in tiles:
            count = self.tile_count(tile.zoom)
            low = max(0, tile.index - radius)
            high = min(count - 1, tile.index + radius)
            expanded.update(TileAddress(tile.zoom, index) for index in range(low, high + 1))
        return sorted(expanded)


@dataclass(frozen=True, slots=True)
class ScreenTransform:
    """데이터 좌표 ↔ 화면 픽셀 변환.

    화면 좌표의 원점은 왼쪽 위다. 픽셀 y는 아래로 갈수록 커지고, 데이터 y는
    위로 갈수록 커지므로 y축만 뒤집는다.
    """

    x_view: XRange
    y_low: float
    y_high: float
    width_px: int
    height_px: int

    def __post_init__(self) -> None:
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError(f"화면 크기는 양수여야 한다: {self.width_px}x{self.height_px}")
        if not self.y_high > self.y_low:
            raise ValueError(f"y_high는 y_low보다 커야 한다: {self.y_low}..{self.y_high}")

    @property
    def x_per_px(self) -> float:
        return self.x_view.width / self.width_px

    @property
    def y_per_px(self) -> float:
        return (self.y_high - self.y_low) / self.height_px

    def to_data(self, px: float, py: float) -> tuple[float, float]:
        """픽셀 좌표 → 데이터 좌표."""
        x = self.x_view.start + px * self.x_per_px
        y = self.y_high - py * self.y_per_px
        return x, y

    def to_pixel(self, x: float, y: float) -> tuple[float, float]:
        """데이터 좌표 → 픽셀 좌표."""
        px = (x - self.x_view.start) / self.x_per_px
        py = (self.y_high - y) / self.y_per_px
        return px, py
