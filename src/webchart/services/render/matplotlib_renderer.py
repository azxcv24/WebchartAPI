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
        self._apply_styling(ax, spec)

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
        """Render a single tile (no axes, no labels, transparent padding)."""
        dpi = 100
        fig = Figure(figsize=(tile_size / dpi, tile_size / dpi), dpi=dpi)
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

    def _apply_styling(self, ax, spec: ChartSpec) -> None:
        if spec.title:
            ax.set_title(spec.title)

        ax.set_xlabel(spec.x_axis.label or spec.x_axis.column)
        if spec.y_axis:
            ax.set_ylabel(spec.y_axis.label or spec.y_axis.column)

        if spec.x_axis.range:
            ax.set_xlim(spec.x_axis.range)
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
