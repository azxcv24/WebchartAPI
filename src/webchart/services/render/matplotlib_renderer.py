"""matplotlib 페인터: 투영된 도형(꺾은선)을 픽셀로 그린다.

pyplot이 아니라 Figure/FigureCanvas 객체 API를 쓴다. pyplot의 전역 figure 관리자를
거치지 않아 렌더 스레드끼리 경합하지 않는다.

두 가지 그림
- `render_raster`: 축 없이 데이터 영역만 정확히 width_px × height_px로 그린다 (타일, 오라클 정답).
  픽셀 열 j = 격자 버킷 j가 되도록 axes가 그림 전체를 덮는다.
- `render_chart`: 축·제목·범례가 있는 완성 차트. 데이터 영역을 정수 픽셀 경계에 맞춰
  (`plot_area_px`) 집계 격자의 버킷 경계와 픽셀 경계가 일치하게 한다.

matplotlib의 경로 단순화(path.simplify)는 끈다. 단순화도 일종의 다운샘플링이라 픽셀 정렬
집계(M4)가 보장한 정확성을 깨뜨릴 수 있고, M4가 이미 점 수를 픽셀당 4개로 줄였기 때문이다.
"""

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import matplotlib
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_svg import FigureCanvasSVG
from matplotlib.figure import Figure

from webchart.domain.bucket_stats import FloatArray
from webchart.domain.geometry import XRange
from webchart.domain.projection import Polyline
from webchart.domain.y_range import YRange
from webchart.models.chart import ChartSpec, ChartType, ImageFormat
from webchart.models.dataset import ColumnInfo, ColumnKind
from webchart.services.data.x_domain import to_canonical

matplotlib.use("Agg")
matplotlib.rcParams["path.simplify"] = False

logger = logging.getLogger("webchart.render.matplotlib")

# 그림 해상도 기준. 1 inch = 100px (dpr 1)
BASE_DPI = 100

# matplotlib 기본 subplot 여백 (figure 대비 비율). plot_area_px가 정수 픽셀로 맞춘다.
_SUBPLOT_LEFT, _SUBPLOT_RIGHT = 0.125, 0.9
_SUBPLOT_BOTTOM, _SUBPLOT_TOP = 0.11, 0.88

DEFAULT_LINE_WIDTH = 1.5


@dataclass(frozen=True, slots=True)
class SeriesLine:
    """그릴 시리즈 하나: 꺾은선(정규 좌표)과 스타일."""

    name: str
    line: Polyline
    color: str | None = None
    label: str | None = None
    line_width: float = DEFAULT_LINE_WIDTH
    opacity: float = 1.0
    marker_size: float | None = None


def series_lines(spec: ChartSpec, lines: dict[str, Polyline]) -> list[SeriesLine]:
    """spec의 시리즈 스타일을 꺾은선에 붙인다 (spec에 적힌 순서 = 기본 색 순서)."""
    if spec.series:
        return [
            SeriesLine(
                name=s.column,
                line=lines[s.column],
                color=s.color,
                label=s.label or s.column,
                line_width=s.line_width,
                opacity=s.opacity,
                marker_size=s.marker_size,
            )
            for s in spec.series
        ]
    return [SeriesLine(name=column, line=lines[column], label=column) for column in lines]


def raw_series_lines(spec: ChartSpec, data: pd.DataFrame, y_columns: list[str]) -> list[SeriesLine]:
    """원본 DataFrame 전체를 그대로 꺾은선으로 (집계 없음, 오라클 정답·소량 데이터용)."""
    x = to_canonical(data[spec.x_axis.column])
    lines = {
        column: Polyline(x, data[column].to_numpy(dtype=np.float64, na_value=np.nan))
        for column in y_columns
    }
    return series_lines(spec, lines)


class MatplotlibRenderer:
    """matplotlib OO API 페인터 (스레드 안전: 호출마다 Figure를 새로 만든다)."""

    # --- 축 없는 래스터 (타일) ------------------------------------------------

    def render_raster(
        self,
        lines: Sequence[SeriesLine],
        chart_type: ChartType,
        x_range: XRange,
        y_range: YRange | None,
        width_px: int,
        height_px: int,
        dpr: int = 1,
        background_color: str = "white",
    ) -> bytes:
        """데이터 영역만 `width_px × height_px` PNG로 그린다.

        y_range가 None이면 matplotlib 자동 스케일을 쓴다 (오라클 정답에서만 쓴다).
        dpr > 1이면 dpi를 올려 선 굵기도 같은 비율로 커진다 (물리 크기 유지).
        """
        dpi = BASE_DPI * dpr
        figure = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
        canvas = FigureCanvasAgg(figure)
        axes = figure.add_axes((0.0, 0.0, 1.0, 1.0))
        axes.set_axis_off()
        figure.patch.set_alpha(0)
        axes.set_facecolor(background_color)

        for series in lines:
            _draw(axes, series, series.line.x, chart_type)
        axes.set_xlim(x_range.start, x_range.end)
        if y_range is not None:
            axes.set_ylim(y_range.low, y_range.high)

        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=dpi)
        del canvas
        return buffer.getvalue()

    # --- 완성 차트 (정적 렌더) -------------------------------------------------

    def plot_area_px(self, spec: ChartSpec) -> tuple[int, int]:
        """완성 차트에서 데이터 영역의 (폭, 높이) 픽셀. 집계 격자의 버킷 수가 된다."""
        left, right, bottom, top = _snapped_subplot_px(spec.width, spec.height)
        return right - left, top - bottom

    def render_chart(
        self,
        spec: ChartSpec,
        lines: Sequence[SeriesLine],
        x_view: XRange | None,
        y_range: YRange | None,
        x_column: ColumnInfo | None = None,
    ) -> bytes:
        """축·제목·범례가 있는 차트를 그린다.

        x_view/y_range가 None이면 matplotlib 자동 스케일을 쓴다 (원본을 그대로 그리는 정답용).
        시간 x축은 정규 좌표(epoch 나노초)를 datetime으로 바꿔 날짜 눈금을 얻는다.
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
        axes = figure.add_subplot(111)
        figure.patch.set_facecolor(spec.background_color)
        axes.set_facecolor(spec.background_color)

        is_time = x_column is not None and x_column.kind is ColumnKind.TIME
        for series in lines:
            x_values = _as_datetime(series.line.x) if is_time else series.line.x
            _draw(axes, series, x_values, spec.chart_type)
        if x_view is not None:
            limits = (x_view.start, x_view.end)
            axes.set_xlim(*(_as_datetime(np.array(limits)) if is_time else limits))
        if y_range is not None:
            axes.set_ylim(y_range.low, y_range.high)
        _apply_styling(axes, spec)

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
        """원본 DataFrame을 집계 없이 그대로 그린다 (ChartRenderer 프로토콜, 오라클 정답).

        서버의 정적 렌더는 이 경로가 아니라 application/static_chart.py의 집계 경로를 쓴다.
        """
        y_columns = spec_y_columns(spec)
        lines = raw_series_lines(spec, data, y_columns)
        x_column = data.get(spec.x_axis.column)
        is_time = x_column is not None and pd.api.types.is_datetime64_any_dtype(x_column)
        info = ColumnInfo(
            name=spec.x_axis.column,
            dtype="datetime" if is_time else "float64",
            kind=ColumnKind.TIME if is_time else ColumnKind.NUMERIC,
        )
        x_view = XRange(*spec.x_axis.range) if spec.x_axis.range else None
        y_range = YRange(*spec.y_axis.range) if spec.y_axis and spec.y_axis.range else None
        return self.render_chart(spec, lines, x_view, y_range, info)

    def supported_formats(self) -> list[str]:
        return ["png", "svg", "webp"]


def spec_y_columns(spec: ChartSpec) -> list[str]:
    """spec에서 y 컬럼 이름 목록 (series → y_columns → y_axis 순으로 본다)."""
    if spec.series:
        return [series.column for series in spec.series]
    if spec.y_columns:
        return list(spec.y_columns)
    if spec.y_axis:
        return [spec.y_axis.column]
    return []


def _snapped_subplot_px(width: int, height: int) -> tuple[int, int, int, int]:
    """기본 subplot 여백을 정수 픽셀로 맞춘 데이터 영역 (left, right, bottom, top)."""
    return (
        round(_SUBPLOT_LEFT * width),
        round(_SUBPLOT_RIGHT * width),
        round(_SUBPLOT_BOTTOM * height),
        round(_SUBPLOT_TOP * height),
    )


def _as_datetime(x: FloatArray) -> np.ndarray[Any, np.dtype[np.datetime64]]:
    """정규 좌표(epoch 나노초) → datetime64[ns]. NaN(선 끊기)은 NaT가 된다."""
    nanoseconds = np.where(np.isnan(x), np.iinfo(np.int64).min, np.round(x)).astype(np.int64)
    return nanoseconds.view("datetime64[ns]")


def _canvas_for(figure: Figure, fmt: ImageFormat) -> FigureCanvasAgg | FigureCanvasSVG:
    if fmt == ImageFormat.SVG:
        return FigureCanvasSVG(figure)
    return FigureCanvasAgg(figure)


def _draw(axes: Axes, series: SeriesLine, x_values: Any, chart_type: ChartType) -> None:
    """시리즈 하나를 차트 종류에 맞게 그린다.

    빈 시리즈도 그린다(점 0개). 그래야 matplotlib 기본 색 순환이 시리즈 순서와 맞는다.
    line 이외의 종류는 Phase 2에서 전용 투영·페인터로 바꾼다.
    """
    kwargs: dict[str, Any] = {"alpha": series.opacity, "label": series.label}
    if series.color:
        kwargs["color"] = series.color
    y_values = series.line.y

    if chart_type == ChartType.BAR:
        axes.bar(x_values, y_values, **kwargs)
    elif chart_type in (ChartType.SCATTER, ChartType.HEATMAP):
        axes.scatter(x_values, y_values, s=(series.marker_size or 3) ** 2, **kwargs)
    else:  # LINE, CANDLESTICK(Phase 2 전까지 line으로 대체)
        if series.marker_size:
            kwargs.update(marker="o", markersize=series.marker_size)
        axes.plot(x_values, y_values, linewidth=series.line_width, **kwargs)


def _apply_styling(axes: Axes, spec: ChartSpec) -> None:
    if spec.title:
        axes.set_title(spec.title)
    axes.set_xlabel(spec.x_axis.label or spec.x_axis.column)
    if spec.y_axis:
        axes.set_ylabel(spec.y_axis.label or spec.y_axis.column)
    if spec.x_axis.log_scale:
        axes.set_xscale("log")
    if spec.y_axis and spec.y_axis.log_scale:
        axes.set_yscale("log")
    if spec.grid:
        axes.grid(True, alpha=0.3)
    if spec.legend and axes.get_legend_handles_labels()[1]:
        # 위치를 데이터에 따라 고르는 "best"는 꼭짓점 수에 따라 결과가 달라져(원본 vs 집계)
        # 같은 데이터가 다른 그림이 될 수 있다. 위치를 고정한다.
        axes.legend(loc="upper right")
