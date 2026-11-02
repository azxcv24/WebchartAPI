"""레이어 레지스트리: 레이어 종류 → 투영 (계획 문서 3.3 Strategy + Registry, D10 해소).

차트 종류를 추가하려면 domain/projection.py에 투영을, services/render/painters.py에
페인터를 만들고 아래 PROJECTIONS에 한 줄 등록한다. 렌더 경로의 다른 코드는 바꾸지 않는다.
"""

from collections.abc import Callable
from dataclasses import dataclass

from webchart.application.windows import WindowSet
from webchart.domain.geometry import BucketGrid
from webchart.domain.projection import (
    Band,
    Bars,
    Candles,
    DensityGrid,
    Polyline,
    aggregate_bars,
    envelope_band,
    m4_polyline,
    ohlc_candles,
    price_candles,
)
from webchart.models.chart import AxisName, ChartSpec, LayerKind, LayerSpec

Shape = Polyline | Band | Bars | Candles | DensityGrid


@dataclass(frozen=True, slots=True)
class LayerDrawing:
    """투영을 마친 레이어: 그릴 도형과 그 레이어 명세."""

    layer: LayerSpec
    shape: Shape

    @property
    def axis(self) -> AxisName:
        return self.layer.axis

    @property
    def point_count(self) -> int:
        if isinstance(self.shape, Polyline):
            return self.shape.point_count
        if isinstance(self.shape, DensityGrid):
            return int(self.shape.counts.sum())
        return len(self.shape)


Projection = Callable[[LayerSpec, WindowSet, int], Shape]


def _line(layer: LayerSpec, windows: WindowSet, dpr: int) -> Shape:
    return m4_polyline(windows.windows[layer.data_columns[0]])


def _band(layer: LayerSpec, windows: WindowSet, dpr: int) -> Shape:
    return envelope_band(windows.windows[layer.data_columns[0]])


def _bar(layer: LayerSpec, windows: WindowSet, dpr: int) -> Shape:
    window = windows.windows[layer.data_columns[0]]
    return aggregate_bars(window, layer.agg.value, _group_columns(layer, window.grid, dpr))


def _candlestick(layer: LayerSpec, windows: WindowSet, dpr: int) -> Shape:
    first = windows.windows[layer.data_columns[0]]
    group = _group_columns(layer, first.grid, dpr)
    if layer.ohlc is None:
        return price_candles(first, group)
    open_, high, low, close = (windows.windows[c] for c in layer.data_columns)
    return ohlc_candles(open_, high, low, close, group)


def _group_columns(layer: LayerSpec, grid: BucketGrid, dpr: int) -> int:
    """막대·캔들 하나가 묶는 픽셀 열 수 (dpr 반영).

    격자 폭이 나눠떨어지지 않으면(정적 렌더의 임의 폭) 나눠떨어질 때까지 절반으로 줄인다.
    """
    wanted = layer.group_px * dpr
    while grid.bucket_count % wanted:
        wanted //= 2  # 정적 렌더처럼 격자 폭이 2의 거듭제곱 배수가 아닐 때
    return max(1, wanted)


# 버킷 집계(BucketStats) 레이어의 투영. 밀도 레이어는 DensityGrid를 그대로 그린다.
PROJECTIONS: dict[LayerKind, Projection] = {
    LayerKind.LINE: _line,
    LayerKind.AREA: _line,
    LayerKind.BAND: _band,
    LayerKind.BAR: _bar,
    LayerKind.CANDLESTICK: _candlestick,
}


def project_layers(
    spec: ChartSpec,
    windows: WindowSet | None,
    densities: dict[int, DensityGrid],
    dpr: int,
) -> list[LayerDrawing]:
    """spec의 레이어를 순서대로 투영한다. densities는 레이어 번호 → 밀도 격자."""
    drawings = []
    for index, layer in enumerate(spec.layers):
        if index in densities:
            drawings.append(LayerDrawing(layer, densities[index]))
        else:
            assert windows is not None
            drawings.append(LayerDrawing(layer, PROJECTIONS[layer.kind](layer, windows, dpr)))
    return drawings


def axis_value_bounds(
    spec: ChartSpec, axis: AxisName, windows: WindowSet, dpr: int
) -> tuple[float, float] | None:
    """축에 붙은 레이어들이 실제로 그릴 y값의 최솟값·최댓값 (보이는 격자 기준).

    - line, band, candlestick, 밀도: 원본 값의 범위 (집계 창의 min/max)
    - area, bar: 0부터 그리므로 0을 포함한다. bar는 집계값(sum 등)의 범위다
    """
    bounds: list[tuple[float, float]] = []
    for layer in spec.layers:
        if layer.axis != axis:
            continue
        if layer.kind == LayerKind.BAR:
            shape = _bar(layer, windows, dpr)
            assert isinstance(shape, Bars)
            layer_bounds = shape.y_bounds()
        else:
            column_bounds = [
                b for c in layer.data_columns if (b := windows.windows[c].stats.y_bounds())
            ]
            layer_bounds = (
                (min(b[0] for b in column_bounds), max(b[1] for b in column_bounds))
                if column_bounds
                else None
            )
            if layer_bounds is not None and layer.kind == LayerKind.AREA:
                layer_bounds = (min(layer_bounds[0], 0.0), max(layer_bounds[1], 0.0))
        if layer_bounds is not None:
            bounds.append(layer_bounds)
    if not bounds:
        return None
    return min(low for low, _ in bounds), max(high for _, high in bounds)
