"""무왜곡 오라클 판정용 래스터 비교.

오라클 테스트의 정답은 원본 데이터 전체를 그린 그림이고, 이 모듈은 파이프라인이
그린 그림과 정답을 비교한다.

안티앨리어싱 때문에 픽셀 값을 그대로 비교하면 의미 없는 차이가 생긴다. 그래서
"잉크가 칠해졌는가"로 이진화한 뒤, 픽셀 열마다 잉크의 위쪽 끝과 아래쪽 끝(열 외곽선)을
비교한다. 피크가 사라지거나 y 스케일이 틀어지면 외곽선이 바로 달라진다.

선 굵기 허용(reach_px): matplotlib은 선을 폭이 있는 다각형으로 칠하므로, 픽셀 열 경계
근처의 가파른 선분은 이웃 열까지 번진다. 원본은 열마다 점이 수백 개라 번지는 모양이
M4(열마다 4점)와 다르다. M4 정리는 폭이 0인 선에 대한 정리이기 때문이다. 그래서 한쪽
그림의 열 외곽선이 다른 쪽 그림의 좌우 reach_px 열 안에서 설명되면 같은 그림으로 본다.
사라진 피크는 자기 열과 이웃 열 모두에서 수백 px 차이가 나므로 이 허용으로 가려지지 않는다.
"""

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

# 배경과의 채널 차이가 이 값 이상이면 잉크로 본다 (0~255).
# 안티앨리어싱으로 반쯤 칠해진 가장자리 픽셀을 걸러 내는 경계값이다.
INK_THRESHOLD = 128

# 열에 잉크가 전혀 없음을 나타내는 외곽선 값
EMPTY_COLUMN = -1


def decode_png(png: bytes) -> np.ndarray:
    """PNG 바이트 → (높이, 너비, 4) uint8 RGBA 배열."""
    with Image.open(io.BytesIO(png)) as image:
        return np.asarray(image.convert("RGBA"))


def ink_mask(
    rgba: np.ndarray, background_rgb: tuple[int, int, int] = (255, 255, 255)
) -> np.ndarray:
    """잉크가 칠해진 픽셀이면 True인 (높이, 너비) bool 배열."""
    rgb = rgba[..., :3].astype(np.int16)
    distance = np.abs(rgb - np.array(background_rgb, dtype=np.int16)).max(axis=-1)
    is_opaque = rgba[..., 3] > 0
    return (distance >= INK_THRESHOLD) & is_opaque


def stitch_horizontally(images: list[np.ndarray]) -> np.ndarray:
    """같은 높이의 이미지들을 왼쪽부터 이어 붙인다 (타일 → 한 장)."""
    heights = {image.shape[0] for image in images}
    if len(heights) != 1:
        raise ValueError(f"이어 붙일 이미지의 높이가 서로 다르다: {sorted(heights)}")
    return np.concatenate(images, axis=1)


@dataclass(frozen=True, slots=True)
class ColumnEnvelope:
    """픽셀 열별 잉크의 위쪽 끝(top)과 아래쪽 끝(bottom) 행 번호. 빈 열은 EMPTY_COLUMN."""

    top: np.ndarray
    bottom: np.ndarray


def column_envelope(mask: np.ndarray) -> ColumnEnvelope:
    height = mask.shape[0]
    has_ink = mask.any(axis=0)
    top = np.where(has_ink, mask.argmax(axis=0), EMPTY_COLUMN)
    bottom = np.where(has_ink, height - 1 - mask[::-1].argmax(axis=0), EMPTY_COLUMN)
    return ColumnEnvelope(top=top, bottom=bottom)


@dataclass(frozen=True, slots=True)
class RasterDiff:
    """정답 대비 결과 그림의 차이.

    Attributes:
        width_px: 비교한 픽셀 열 수
        mismatched_columns: 외곽선 차이가 허용 오차를 넘은 열 수
        max_deviation_px: 가장 크게 어긋난 열의 차이 (한쪽만 빈 열이면 이미지 높이)
        ink_diff_ratio: 잉크 여부가 다른 픽셀 비율 (참고용)
    """

    width_px: int
    mismatched_columns: int
    max_deviation_px: int
    ink_diff_ratio: float

    @property
    def mismatch_ratio(self) -> float:
        return self.mismatched_columns / self.width_px

    def summary(self) -> str:
        return (
            f"어긋난 열 {self.mismatched_columns}/{self.width_px} "
            f"({self.mismatch_ratio:.1%}), 최대 {self.max_deviation_px}px, "
            f"잉크 차이 {self.ink_diff_ratio:.2%}"
        )


def compare_envelopes(
    expected: np.ndarray, actual: np.ndarray, tolerance_px: int = 1, reach_px: int = 0
) -> RasterDiff:
    """두 잉크 마스크의 열 외곽선을 비교한다.

    열 j의 잉크 위쪽 끝은 상대 그림의 열 [j - reach_px, j + reach_px] 중 가장 높은
    위쪽 끝보다 tolerance_px 넘게 높으면 안 된다 (아래쪽 끝도 같은 규칙). 양방향으로 검사한다.
    상대 그림의 그 범위에 잉크가 전혀 없으면 이미지 높이만큼 어긋난 것으로 본다.
    """
    if expected.shape != actual.shape:
        raise ValueError(f"그림 크기가 다르다: 정답 {expected.shape}, 결과 {actual.shape}")
    height, width = expected.shape
    want = column_envelope(expected)
    got = column_envelope(actual)

    deviation = np.maximum(
        _uncovered_extent(want, got, reach_px, height),
        _uncovered_extent(got, want, reach_px, height),
    )
    return RasterDiff(
        width_px=width,
        mismatched_columns=int((deviation > tolerance_px).sum()),
        max_deviation_px=int(deviation.max(initial=0)),
        ink_diff_ratio=float((expected != actual).mean()),
    )


def _uncovered_extent(
    subject: ColumnEnvelope, other: ColumnEnvelope, reach_px: int, height: int
) -> np.ndarray:
    """subject의 열 외곽선이 other의 이웃 열 외곽선 바깥으로 벗어난 정도 (열별 px)."""
    has_ink = subject.top != EMPTY_COLUMN
    other_has_ink = other.top != EMPTY_COLUMN
    # 잉크 없는 열은 "가장 높은 위쪽 끝/가장 낮은 아래쪽 끝" 계산에서 빠지도록 극단값을 둔다
    other_top = _sliding(np.where(other_has_ink, other.top, height * 2), reach_px, np.min)
    other_bottom = _sliding(np.where(other_has_ink, other.bottom, -height), reach_px, np.max)
    above = other_top - subject.top
    below = subject.bottom - other_bottom
    deviation = np.clip(np.maximum(above, below), 0, height)
    return np.where(has_ink, deviation, 0)


def _sliding(values: np.ndarray, reach_px: int, reduce) -> np.ndarray:
    """각 열 j에 대해 values[j - reach_px : j + reach_px + 1]을 reduce한 값 (가장자리는 복제)."""
    if reach_px == 0:
        return values
    padded = np.pad(values, reach_px, mode="edge")
    shifted = [padded[offset : offset + len(values)] for offset in range(2 * reach_px + 1)]
    return reduce(np.stack(shifted), axis=0)
