"""페인터 레지스트리: 도형 종류 → matplotlib 그리기 함수 (계획 문서 3.3, D10 해소).

투영(domain/projection.py)이 만든 도형을 그린다. 모든 x는 정규 좌표(시간축은 epoch
나노초)이고, 축 눈금은 별도 서비스(application/axes.py)가 붙인다.

색: 레이어 순서 i에 matplotlib 기본 색 `C{i}`를 고정으로 준다. y2축은 별도 axes라
기본 색 순환이 다시 시작되므로, 순환에 맡기지 않고 레이어 번호로 정한다.
"""

from collections.abc import Callable
from typing import Any

import matplotlib
import numpy as np
from matplotlib.axes import Axes
from matplotlib.colors import to_rgba

from webchart.domain.bucket_stats import FloatArray
from webchart.domain.projection import (
    Band,
    Bars,
    Candles,
    DensityGrid,
    Polyline,
    Spans,
    normalize_density,
)
from webchart.models.chart import LayerKind, LayerSpec

# 막대·캔들 몸통이 구간 폭에서 차지하는 비율 (나머지는 막대 사이 여백)
BAR_FILL_RATIO = 0.8
CANDLE_UP_COLOR = "#26a69a"
CANDLE_DOWN_COLOR = "#ef5350"
# area 채우기 투명도 (선 투명도에 곱한다)
AREA_FILL_ALPHA = 0.3
# scatter 밀도에서 점 1개 픽셀의 최소 불투명도 (0이면 개수 1인 픽셀이 안 보인다)
SCATTER_MIN_ALPHA = 0.2

Painter = Callable[[Axes, Any, LayerSpec, str], None]


def layer_color(layer: LayerSpec, index: int) -> str:
    return layer.style.color or f"C{index}"


def paint(axes: Axes, shape: object, layer: LayerSpec, color: str) -> None:
    """도형 종류에 맞는 페인터를 찾아 그린다."""
    PAINTERS[type(shape)](axes, shape, layer, color)


def _paint_polyline(axes: Axes, line: Polyline, layer: LayerSpec, color: str) -> None:
    style = layer.style
    kwargs: dict[str, Any] = {"color": color, "alpha": style.opacity, "label": layer.label}
    if style.marker_size:
        kwargs.update(marker="o", markersize=style.marker_size)
    axes.plot(line.x, line.y, linewidth=style.line_width, **kwargs)
    if layer.kind == LayerKind.AREA:
        axes.fill_between(
            line.x, line.y, 0.0, color=color, alpha=style.opacity * AREA_FILL_ALPHA, linewidth=0
        )


def _paint_band(axes: Axes, band: Band, layer: LayerSpec, color: str) -> None:
    x, low = _steps(band, band.low)
    _, high = _steps(band, band.high)
    axes.fill_between(
        x, low, high, color=color, alpha=layer.style.opacity, linewidth=0, label=layer.label
    )


def _steps(spans: Spans, values: FloatArray) -> tuple[FloatArray, FloatArray]:
    """구간 값 → 계단 꺾은선 (구간마다 왼쪽·오른쪽 두 점). 이어지지 않는 구간 사이는 NaN."""
    x = np.column_stack([spans.left_x, spans.right_x, np.full(len(spans), np.nan)])
    y = np.column_stack([values, values, np.full(len(spans), np.nan)])
    is_continuous = np.append(spans.left_x[1:] == spans.right_x[:-1], False)
    keep = np.ones(x.shape, dtype=bool)
    keep[:, 2] = ~is_continuous
    return x[keep], y[keep]


def _paint_bars(axes: Axes, bars: Bars, layer: LayerSpec, color: str) -> None:
    width = (bars.right_x - bars.left_x) * BAR_FILL_RATIO
    offset = (bars.right_x - bars.left_x) * (1 - BAR_FILL_RATIO) / 2
    axes.bar(
        bars.left_x + offset,
        bars.value,
        width=width,
        align="edge",
        color=color,
        alpha=layer.style.opacity,
        label=layer.label,
    )


def _paint_candles(axes: Axes, candles: Candles, layer: LayerSpec, color: str) -> None:
    center = (candles.left_x + candles.right_x) / 2
    is_up = candles.close >= candles.open
    colors = np.where(is_up, layer.style.color or CANDLE_UP_COLOR, CANDLE_DOWN_COLOR)
    axes.vlines(center, candles.low, candles.high, colors=list(colors), linewidth=1)
    width = (candles.right_x - candles.left_x) * BAR_FILL_RATIO
    body = candles.close - candles.open
    axes.bar(
        center - width / 2,
        np.where(body == 0, np.finfo(float).tiny, body),
        bottom=candles.open,
        width=width,
        align="edge",
        color=list(colors),
        alpha=layer.style.opacity,
        label=layer.label,
    )


def _paint_density(axes: Axes, density: DensityGrid, layer: LayerSpec, color: str) -> None:
    """밀도 격자를 픽셀 하나 = 셀 하나로 그린다 (보간 없음)."""
    intensity = normalize_density(density.counts, layer.normalization.value)
    occupied = density.counts > 0
    rgba = np.zeros((*density.counts.shape, 4))
    if layer.kind == LayerKind.HEATMAP:
        rgba[:] = matplotlib.colormaps[layer.style.colormap](intensity)
        rgba[..., 3] = np.where(occupied, layer.style.opacity, 0.0)
    else:
        rgba[..., :3] = to_rgba(color)[:3]
        alpha = SCATTER_MIN_ALPHA + (1 - SCATTER_MIN_ALPHA) * intensity
        rgba[..., 3] = np.where(occupied, alpha * layer.style.opacity, 0.0)
    axes.imshow(
        rgba,
        extent=(density.grid.origin, density.grid.end, density.y_low, density.y_high),
        origin="lower",
        interpolation="nearest",
        aspect="auto",
        resample=False,
    )


PAINTERS: dict[type, Painter] = {
    Polyline: _paint_polyline,
    Band: _paint_band,
    Bars: _paint_bars,
    Candles: _paint_candles,
    DensityGrid: _paint_density,
}
