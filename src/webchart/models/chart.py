from enum import Enum

from pydantic import BaseModel, Field


class ChartType(str, Enum):
    LINE = "line"
    BAR = "bar"
    SCATTER = "scatter"
    HEATMAP = "heatmap"
    CANDLESTICK = "candlestick"


class ImageFormat(str, Enum):
    PNG = "png"
    SVG = "svg"
    WEBP = "webp"


class DownsampleAlgorithm(str, Enum):
    LTTB = "lttb"
    MINMAX = "minmax"
    AVERAGE = "average"


class AxisConfig(BaseModel):
    column: str
    label: str | None = None
    range: tuple[float, float] | None = None
    log_scale: bool = False


class SeriesConfig(BaseModel):
    column: str
    label: str | None = None
    color: str | None = None
    line_width: float = 1.5
    marker_size: float | None = None
    opacity: float = 1.0


class ChartSpec(BaseModel):
    dataset_id: str
    chart_type: ChartType = ChartType.LINE
    title: str | None = None
    x_axis: AxisConfig
    y_axis: AxisConfig | None = None
    y_columns: list[str] = Field(default_factory=list)
    series: list[SeriesConfig] = Field(default_factory=list)
    width: int = Field(default=1200, ge=64, le=8192)
    height: int = Field(default=600, ge=64, le=8192)
    format: ImageFormat = ImageFormat.PNG
    background_color: str = "#ffffff"
    grid: bool = True
    legend: bool = True
    downsample_algorithm: DownsampleAlgorithm = DownsampleAlgorithm.LTTB


class RenderResult(BaseModel):
    render_id: str
    format: ImageFormat
    width: int
    height: int
    size_bytes: int
