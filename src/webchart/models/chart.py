from enum import Enum

from pydantic import BaseModel, Field

from webchart.domain.y_range import YRangePolicy


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
    """점 줄이기 방식.

    M4(기본)는 픽셀 열별 first/min/max/last로, 원본 전체를 그린 그림과 같다 (무왜곡).
    나머지는 이전 버전 호환용 근사다. 고르면 응답이 exact=false가 된다.
    """

    M4 = "m4"
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
    downsample_algorithm: DownsampleAlgorithm = DownsampleAlgorithm.M4
    # y범위를 정하는 방법. None이면 y_axis.range가 있을 때 fixed, 없으면 경로별 기본값
    # (타일: global, 정적 렌더: viewport). 계획 문서 2.5.
    y_range_policy: YRangePolicy | None = None


class RenderResult(BaseModel):
    render_id: str
    format: ImageFormat
    width: int
    height: int
    size_bytes: int
    is_exact: bool = True  # 원본 전체를 그린 그림과 같음을 보증하는지 (P3)
    source: str = ""  # "raw" 또는 "pyramid:L{레벨}"
    point_count: int = 0
