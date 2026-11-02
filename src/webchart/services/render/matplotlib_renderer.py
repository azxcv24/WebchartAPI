"""Matplotlib-based chart renderer for static images and tiles.

Uses the object-oriented Figure/FigureCanvas API (not pyplot) so multiple
renderer instances and threads do not contend on pyplot's global figure
manager. With Agg/SVG canvases the per-figure work is independent.
"""

import io
import logging

import matplotlib
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.backends.backend_svg import FigureCanvasSVG
from matplotlib.figure import Figure

from webchart.models.chart import ChartSpec, ChartType, ImageFormat

matplotlib.use("Agg")  # Default backend; we still construct canvases explicitly

logger = logging.getLogger("webchart.render.matplotlib")


def _canvas_for(fig: Figure, fmt: ImageFormat) -> FigureCanvasAgg | FigureCanvasSVG:
    if fmt == ImageFormat.SVG:
        return FigureCanvasSVG(fig)
    return FigureCanvasAgg(fig)


def _x_limits_in_axis_units(
    x_range: tuple[float, float], x_values: "pd.Series | None"
) -> tuple[object, object]:
    """요청의 x 범위(정규 좌표)를 matplotlib 축 단위로 바꾼다.

    정적 렌더는 시간 x를 datetime 그대로 그려 날짜 눈금을 얻는다. 이때 축 단위는
    datetime이므로 epoch 나노초 범위를 Timestamp로 바꿔야 한다.
    """
    if x_values is None or not pd.api.types.is_datetime64_any_dtype(x_values):
        return x_range
    timezone = x_values.dt.tz
    low, high = (pd.Timestamp(round(bound), unit="ns") for bound in x_range)
    if timezone is not None:
        low, high = low.tz_localize("UTC"), high.tz_localize("UTC")
    return low, high


class MatplotlibRenderer:
    """Renders charts using Matplotlib via the OO API (thread-safe)."""

    def render(self, spec: ChartSpec, data: pd.DataFrame) -> bytes:
        """Render a full chart to image bytes."""
        fig = Figure(figsize=(spec.width / 100, spec.height / 100), dpi=100)
        canvas = _canvas_for(fig, spec.format)
        ax = fig.add_subplot(111)
        fig.patch.set_facecolor(spec.background_color)
        ax.set_facecolor(spec.background_color)

        self._draw_chart(ax, spec, data)
        self._apply_styling(ax, spec, data)

        buf = io.BytesIO()
        fmt = self._to_mpl_format(spec.format)
        fig.savefig(buf, format=fmt, bbox_inches="tight", facecolor=fig.get_facecolor())
        del canvas

        buf.seek(0)
        return buf.read()

    def render_tile(
        self,
        data: pd.DataFrame,
        x_column: str,
        y_columns: list[str],
        chart_type: ChartType,
        tile_size: int = 512,
        x_range: tuple[float, float] | None = None,
        y_range: tuple[float, float] | None = None,
    ) -> bytes:
        """타일 하나를 그린다. 타일은 정사각형 `render_raster`이다."""
        return self.render_raster(
            data, x_column, y_columns, chart_type,
            width_px=tile_size, height_px=tile_size, x_range=x_range, y_range=y_range,
        )

    def render_raster(
        self,
        data: pd.DataFrame,
        x_column: str,
        y_columns: list[str],
        chart_type: ChartType,
        width_px: int,
        height_px: int,
        x_range: tuple[float, float] | None = None,
        y_range: tuple[float, float] | None = None,
    ) -> bytes:
        """축·라벨 없이 데이터 영역만 `width_px × height_px` PNG로 그린다.

        타일 렌더와 오라클 테스트의 정답 렌더가 같은 painter를 쓰도록 공개한다.
        y_range가 None이면 matplotlib 자동 스케일(데이터 범위 + 여백 5%)을 쓴다.
        """
        dpi = 100
        fig = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
        canvas = FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        fig.patch.set_alpha(0)
        ax.set_facecolor("white")

        ax.set_axis_off()
        fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

        if not data.empty:
            for y_col in y_columns:
                if y_col in data.columns:
                    self._draw_series(ax, data, x_column, y_col, chart_type)

        if x_range:
            ax.set_xlim(x_range)
        if y_range:
            ax.set_ylim(y_range)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0)
        del canvas

        buf.seek(0)
        return buf.read()

    def _draw_chart(self, ax, spec: ChartSpec, data: pd.DataFrame) -> None:
        x_col = spec.x_axis.column

        if spec.series:
            for s in spec.series:
                self._draw_series(
                    ax, data, x_col, s.column, spec.chart_type,
                    color=s.color, label=s.label or s.column,
                    linewidth=s.line_width, alpha=s.opacity,
                    marker_size=s.marker_size,
                )
        elif spec.y_columns:
            for y_col in spec.y_columns:
                self._draw_series(ax, data, x_col, y_col, spec.chart_type, label=y_col)
        elif spec.y_axis:
            self._draw_series(ax, data, x_col, spec.y_axis.column, spec.chart_type)

    def _draw_series(
        self,
        ax,
        data: pd.DataFrame,
        x_col: str,
        y_col: str,
        chart_type: ChartType,
        color: str | None = None,
        label: str | None = None,
        linewidth: float = 1.5,
        alpha: float = 1.0,
        marker_size: float | None = None,
    ) -> None:
        if x_col not in data.columns or y_col not in data.columns:
            return

        x = data[x_col]
        y = data[y_col]

        kwargs: dict = {"alpha": alpha}
        if color:
            kwargs["color"] = color
        if label:
            kwargs["label"] = label

        if chart_type == ChartType.LINE:
            kwargs["linewidth"] = linewidth
            if marker_size:
                kwargs["markersize"] = marker_size
                kwargs["marker"] = "o"
            ax.plot(x, y, **kwargs)

        elif chart_type == ChartType.BAR:
            ax.bar(x, y, **kwargs)

        elif chart_type == ChartType.SCATTER:
            s = (marker_size or 3) ** 2
            ax.scatter(x, y, s=s, **kwargs)

        elif chart_type == ChartType.HEATMAP:
            logger.warning("Heatmap requires pivoted data, falling back to scatter")
            ax.scatter(x, y, **kwargs)

        elif chart_type == ChartType.CANDLESTICK:
            logger.warning("Candlestick requires OHLC columns, falling back to line")
            ax.plot(x, y, **kwargs)

    def _apply_styling(self, ax, spec: ChartSpec, data: pd.DataFrame) -> None:
        if spec.title:
            ax.set_title(spec.title)

        ax.set_xlabel(spec.x_axis.label or spec.x_axis.column)
        if spec.y_axis:
            ax.set_ylabel(spec.y_axis.label or spec.y_axis.column)

        if spec.x_axis.range:
            ax.set_xlim(_x_limits_in_axis_units(spec.x_axis.range, data.get(spec.x_axis.column)))
        if spec.y_axis and spec.y_axis.range:
            ax.set_ylim(spec.y_axis.range)

        if spec.x_axis.log_scale:
            ax.set_xscale("log")
        if spec.y_axis and spec.y_axis.log_scale:
            ax.set_yscale("log")

        if spec.grid:
            ax.grid(True, alpha=0.3)

        if spec.legend and ax.get_legend_handles_labels()[1]:
            ax.legend()

    def supported_formats(self) -> list[str]:
        return ["png", "svg", "webp"]

    @staticmethod
    def _to_mpl_format(fmt: ImageFormat) -> str:
        return fmt.value
