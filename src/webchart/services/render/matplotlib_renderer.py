"""matplotlib 렌더러: 레이어 그림(LayerDrawing)을 픽셀로 그린다.

pyplot이 아니라 Figure/FigureCanvas 객체 API를 쓴다. pyplot의 전역 figure 관리자를
거치지 않아 렌더 스레드끼리 경합하지 않는다. 도형별 그리기는 painters.py가 맡는다.

두 가지 그림
- `render_raster`: 축 없이 데이터 영역만 정확히 width_px × height_px로 그린다 (타일, 오라클 정답).
  픽셀 열 j = 격자 버킷 j가 되도록 axes가 그림 전체를 덮는다.
- `render_chart`: 축·제목·범례가 있는 완성 차트. 데이터 영역을 정수 픽셀 경계에 맞춰
  (`plot_area_px`) 집계 격자의 버킷 경계와 픽셀 경계가 일치하게 한다. 눈금은 서버의
  눈금 서비스(application/axes.py)가 정한 값을 그대로 쓴다.

y2축은 같은 x를 공유하는 두 번째 axes(twinx)에 그린다. y축 레이어가 y2축 레이어 위에 그려진다.

matplotlib의 경로 단순화(path.simplify)는 끈다. 단순화도 일종의 다운샘플링이라 픽셀 정렬
집계(M4)가 보장한 정확성을 깨뜨릴 수 있고, M4가 이미 점 수를 픽셀당 4개로 줄였기 때문이다.
"""

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import matplotlib
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_svg import FigureCanvasSVG
from matplotlib.figure import Figure

from webchart.domain.geometry import XRange
from webchart.domain.projection import Polyline
from webchart.domain.ticks import Tick
from webchart.domain.y_range import YRange
from webchart.models.chart import AxisName, ChartSpec, ImageFormat, LayerKind, LayerSpec
from webchart.services.data.x_domain import to_canonical
from webchart.services.render.painters import layer_color, paint

matplotlib.use("Agg")
matplotlib.rcParams["path.simplify"] = False

# 그림 해상도 기준. 1 inch = 100px (dpr 1)
BASE_DPI = 100

# matplotlib 기본 subplot 여백 (figure 대비 비율). plot_area_px가 정수 픽셀로 맞춘다.
_SUBPLOT_LEFT, _SUBPLOT_RIGHT = 0.125, 0.9
_SUBPLOT_BOTTOM, _SUBPLOT_TOP = 0.11, 0.88


class Drawing(Protocol):
    """그릴 레이어 하나 (application/layers.py의 LayerDrawing)."""

    @property
    def layer(self) -> LayerSpec: ...

    @property
    def shape(self) -> object: ...


@dataclass(frozen=True, slots=True)
class RawDrawing:
    """집계 없이 원본 점을 그대로 그리는 레이어 (오라클 정답, 소량 데이터용)."""

    layer: LayerSpec
    shape: object


@dataclass(frozen=True, slots=True)
class AxisTicks:
    """완성 차트의 축 눈금 (서버가 계산한 값)."""

    x: list[Tick] = field(default_factory=list)
    y: dict[AxisName, list[Tick]] = field(default_factory=dict)


def raw_drawings(spec: ChartSpec, data: pd.DataFrame) -> list[RawDrawing]:
    """원본 DataFrame 전체를 그대로 그릴 레이어들 (line, area만 지원)."""
    x = to_canonical(data[spec.x_axis.column])
    drawings = []
    for layer in spec.layers:
        if layer.kind not in (LayerKind.LINE, LayerKind.AREA):
            raise ValueError(f"원본 직접 그리기는 line/area만 지원한다: {layer.kind.value}")
        y = data[layer.data_columns[0]].to_numpy(dtype=np.float64, na_value=np.nan)
        drawings.append(RawDrawing(layer, Polyline(x, y)))
    return drawings


class MatplotlibRenderer:
    """matplotlib OO API 렌더러 (스레드 안전: 호출마다 Figure를 새로 만든다)."""

    # --- 축 없는 래스터 (타일) ------------------------------------------------

    def render_raster(
        self,
        drawings: Sequence[Drawing],
        x_range: XRange,
        y_ranges: dict[AxisName, YRange] | None,
        width_px: int,
        height_px: int,
        dpr: int = 1,
        background_color: str = "white",
    ) -> bytes:
        """데이터 영역만 `width_px × height_px` PNG로 그린다.

        y_ranges가 None이면 matplotlib 자동 스케일을 쓴다 (오라클 정답에서만 쓴다).
        dpr > 1이면 dpi를 올려 선 굵기도 같은 비율로 커진다 (물리 크기 유지).
        """
        dpi = BASE_DPI * dpr
        figure = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
        canvas = FigureCanvasAgg(figure)
        figure.patch.set_alpha(0)
        main = figure.add_axes((0.0, 0.0, 1.0, 1.0))
        main.set_facecolor(background_color)
        axes = _axes_by_name(main, drawings)
        for each in axes.values():
            each.set_axis_off()

        _paint_all(axes, drawings)
        for name, each in axes.items():
            each.set_xlim(x_range.start, x_range.end)
            if y_ranges is not None and name in y_ranges:
                each.set_ylim(y_ranges[name].low, y_ranges[name].high)

        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=dpi)
        del canvas
        return buffer.getvalue()

    # --- 완성 차트 (정적 렌더) -------------------------------------------------

    def plot_area_px(self, spec: ChartSpec) -> tuple[int, int]:
        """완성 차트에서 데이터 영역의 (폭, 높이) 픽셀. 집계 격자의 크기가 된다."""
        left, right, bottom, top = _snapped_subplot_px(spec.width, spec.height)
        return right - left, top - bottom

    def render_chart(
        self,
        spec: ChartSpec,
        drawings: Sequence[Drawing],
        x_view: XRange | None,
        y_ranges: dict[AxisName, YRange] | None,
        ticks: AxisTicks | None = None,
    ) -> bytes:
        """축·제목·범례가 있는 차트를 그린다.

        x_view/y_ranges가 None이면 matplotlib 자동 스케일을 쓴다 (원본을 그대로 그리는 정답용).
        ticks가 없으면 matplotlib 기본 눈금을 쓴다.
        """
        figure = Figure(figsize=(spec.width / BASE_DPI, spec.height / BASE_DPI), dpi=BASE_DPI)
        canvas = _canvas_for(figure, spec.format)
        left, right, bottom, top = _snapped_subplot_px(spec.width, spec.height)
        figure.subplots_adjust(
            left=left / spec.width,
            right=right / spec.width,
            bottom=bottom / spec.height,
            top=top / spec.height,
        )
        main = figure.add_subplot(111)
        figure.patch.set_facecolor(spec.background_color)
        main.set_facecolor(spec.background_color)
        axes = _axes_by_name(main, drawings)

        _paint_all(axes, drawings)
        for name, each in axes.items():
            if x_view is not None:
                each.set_xlim(x_view.start, x_view.end)
            if y_ranges is not None and name in y_ranges:
                each.set_ylim(y_ranges[name].low, y_ranges[name].high)
        _apply_styling(axes, spec, ticks)

        buffer = io.BytesIO()
        figure.savefig(
            buffer,
            format=spec.format.value,
            bbox_inches="tight",
            facecolor=figure.get_facecolor(),
        )
        del canvas
        return buffer.getvalue()

    def render(self, spec: ChartSpec, data: pd.DataFrame) -> bytes:
        """원본 DataFrame을 집계 없이 그대로 그린다 (ChartRenderer 프로토콜).

        축 범위와 눈금은 matplotlib 기본값이다. 서버의 정적 렌더는 이 경로가 아니라
        application/static_chart.py의 집계 경로를 쓴다.
        """
        x_view = XRange(*spec.x_axis.range) if spec.x_axis.range else None
        axis = spec.axis("y")
        y_ranges: dict[AxisName, YRange] | None = {"y": YRange(*axis.range)} if axis.range else None
        return self.render_chart(spec, raw_drawings(spec, data), x_view, y_ranges)

    def supported_formats(self) -> list[str]:
        return ["png", "svg", "webp"]


def _axes_by_name(main: Axes, drawings: Sequence[Drawing]) -> dict[AxisName, Axes]:
    """y축 이름 → axes. y2 레이어가 있으면 x를 공유하는 두 번째 axes를 만든다."""
    axes: dict[AxisName, Axes] = {"y": main}
    if any(drawing.layer.axis == "y2" for drawing in drawings):
        secondary = main.twinx()
        # twinx는 주 axes 위에 그려진다. 보조축(거래량 막대 등)이 주축 선을 가리지 않게
        # 주 axes를 위로 올리고, 배경은 아래쪽(보조) axes가 칠한다.
        main.set_zorder(secondary.get_zorder() + 1)
        secondary.patch.set_visible(True)
        secondary.patch.set_facecolor(main.get_facecolor())
        main.patch.set_visible(False)
        axes["y2"] = secondary
    return axes


def _paint_all(axes: dict[AxisName, Axes], drawings: Sequence[Drawing]) -> None:
    for index, drawing in enumerate(drawings):
        target = axes.get(drawing.layer.axis, axes["y"])
        paint(target, drawing.shape, drawing.layer, layer_color(drawing.layer, index))


def _snapped_subplot_px(width: int, height: int) -> tuple[int, int, int, int]:
    """기본 subplot 여백을 정수 픽셀로 맞춘 데이터 영역 (left, right, bottom, top)."""
    return (
        round(_SUBPLOT_LEFT * width),
        round(_SUBPLOT_RIGHT * width),
        round(_SUBPLOT_BOTTOM * height),
        round(_SUBPLOT_TOP * height),
    )


def _canvas_for(figure: Figure, fmt: ImageFormat) -> FigureCanvasAgg | FigureCanvasSVG:
    if fmt == ImageFormat.SVG:
        return FigureCanvasSVG(figure)
    return FigureCanvasAgg(figure)


def _apply_styling(axes: dict[AxisName, Axes], spec: ChartSpec, ticks: AxisTicks | None) -> None:
    main = axes["y"]
    if spec.title:
        main.set_title(spec.title)
    main.set_xlabel(spec.x_axis.label or spec.x_axis.column)
    if spec.x_axis.log_scale:
        main.set_xscale("log")
    elif ticks is not None and ticks.x:
        main.set_xticks([t.value for t in ticks.x], [t.label for t in ticks.x])

    for name, each in axes.items():
        axis = spec.axis(name)
        if axis.label:
            each.set_ylabel(axis.label)
        if axis.log_scale:
            each.set_yscale("log")
        elif ticks is not None and name in ticks.y:
            each.set_yticks([t.value for t in ticks.y[name]], [t.label for t in ticks.y[name]])

    if spec.grid:
        main.grid(True, alpha=0.3)
    if spec.legend:
        handles, labels = [], []
        for each in axes.values():
            more_handles, more_labels = each.get_legend_handles_labels()
            handles += more_handles
            labels += more_labels
        if labels:
            # 위치를 데이터에 따라 고르는 "best"는 꼭짓점 수에 따라 결과가 달라져(원본 vs 집계)
            # 같은 데이터가 다른 그림이 될 수 있다. 위치를 고정한다.
            main.legend(handles, labels, loc="upper right")
