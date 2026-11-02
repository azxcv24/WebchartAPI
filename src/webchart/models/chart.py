"""차트 명세 (API 경계 모델, 계획 문서 4.1).

ChartSpec v2는 **레이어 + 축**으로 차트를 표현한다.

    layers: [{kind: candlestick, column: price, axis: y},
             {kind: line, column: ma20, axis: y},
             {kind: bar, column: volume, agg: sum, axis: y2}]
    axes:   {y: {label: 가격}, y2: {label: 거래량, range_policy: viewport}}

v1 필드(`chart_type`, `y_columns`, `series`, `y_axis`)도 그대로 받는다. `layers`가 비어
있으면 검증기가 v1 필드를 레이어로 바꾼다 (어댑터). 이후 코드는 레이어만 본다.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from webchart.domain.y_range import YRangePolicy


class ChartType(str, Enum):
    """v1 차트 종류 (레이어 종류로 바뀐다)."""

    LINE = "line"
    BAR = "bar"
    SCATTER = "scatter"
    HEATMAP = "heatmap"
    CANDLESTICK = "candlestick"


class LayerKind(str, Enum):
    """레이어 종류. 종류마다 집계 투영과 페인터가 정해져 있다 (application/layers.py)."""

    LINE = "line"  # M4 꺾은선
    AREA = "area"  # M4 꺾은선 아래를 0까지 채움
    BAND = "band"  # 픽셀 열별 min~max 띠
    BAR = "bar"  # 구간별 집계값(agg) 막대
    CANDLESTICK = "candlestick"  # 구간별 OHLC
    SCATTER = "scatter"  # 점 밀도 (단색 농도)
    HEATMAP = "heatmap"  # 점 밀도 (컬러맵)


# 버킷 집계(BucketStats)를 쓰는 레이어. 나머지(scatter, heatmap)는 2D 밀도 격자를 쓴다.
BUCKET_LAYER_KINDS = frozenset(
    {LayerKind.LINE, LayerKind.AREA, LayerKind.BAND, LayerKind.BAR, LayerKind.CANDLESTICK}
)
DENSITY_LAYER_KINDS = frozenset({LayerKind.SCATTER, LayerKind.HEATMAP})


class BarAggregate(str, Enum):
    """막대 하나(x 구간)의 값을 만드는 집계. 어떤 집계인지 API에 명시한다 (계획 문서 2.3)."""

    SUM = "sum"
    MEAN = "mean"
    COUNT = "count"
    MIN = "min"
    MAX = "max"


class DensityNormalization(str, Enum):
    """픽셀별 점 개수 → 색 농도 변환 (Datashader 방식)."""

    LINEAR = "linear"
    LOG = "log"
    EQ_HIST = "eq_hist"  # 개수 순위로 고르게 펼친다 (분포 모양이 가장 잘 보인다)


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


AxisName = Literal["y", "y2"]


class OhlcColumns(BaseModel):
    """이미 만들어진 OHLC 컬럼. 구간 병합: first(open), max(high), min(low), last(close)."""

    open: str
    high: str
    low: str
    close: str


class LayerStyle(BaseModel):
    color: str | None = None
    label: str | None = None
    line_width: float = Field(default=1.5, gt=0, le=20)
    opacity: float = Field(default=1.0, ge=0, le=1)
    marker_size: float | None = None
    colormap: str = "viridis"  # heatmap 전용


class LayerSpec(BaseModel):
    """레이어 하나.

    Attributes:
        kind: 레이어 종류
        column: 값 컬럼. candlestick은 원시 가격 컬럼 또는 `ohlc` 중 하나를 쓴다
        ohlc: 이미 만들어진 OHLC 컬럼 (candlestick)
        axis: 붙을 y축
        agg: 막대 집계 (bar)
        group_px: 막대·캔들 하나의 폭(dpr 1 기준 픽셀, 2의 거듭제곱). 타일 경계와 맞도록
            타일 폭을 나눠떨어지게 한다
        normalization: 밀도 농도 변환 (scatter, heatmap)
    """

    kind: LayerKind
    column: str | None = None
    ohlc: OhlcColumns | None = None
    axis: AxisName = "y"
    agg: BarAggregate = BarAggregate.MEAN
    group_px: int = Field(default=8, ge=1, le=256)
    normalization: DensityNormalization = DensityNormalization.EQ_HIST
    style: LayerStyle = Field(default_factory=LayerStyle)

    @model_validator(mode="after")
    def _check_columns(self) -> "LayerSpec":
        if self.kind == LayerKind.CANDLESTICK:
            if (self.column is None) == (self.ohlc is None):
                raise ValueError("candlestick은 column과 ohlc 중 하나만 지정한다")
        elif self.column is None:
            raise ValueError(f"{self.kind.value} 레이어에는 column이 필요하다")
        if self.group_px & (self.group_px - 1):
            raise ValueError(f"group_px는 2의 거듭제곱이어야 한다: {self.group_px}")
        return self

    @property
    def data_columns(self) -> list[str]:
        """이 레이어가 읽는 y 컬럼들."""
        if self.ohlc is not None:
            return [self.ohlc.open, self.ohlc.high, self.ohlc.low, self.ohlc.close]
        assert self.column is not None
        return [self.column]

    @property
    def label(self) -> str:
        return self.style.label or self.column or (self.ohlc.close if self.ohlc else "")


class AxisConfig(BaseModel):
    """v1 축 설정 (x축과 v1 y축에 쓴다)."""

    column: str
    label: str | None = None
    range: tuple[float, float] | None = None
    log_scale: bool = False
    # 시간 x축 눈금을 표시할 시간대 (예: "Asia/Seoul"). None이면 컬럼의 원래 시간대
    timezone: str | None = None


class YAxisSpec(BaseModel):
    """v2 y축 설정."""

    label: str | None = None
    range: tuple[float, float] | None = None  # 지정하면 fixed 정책
    range_policy: YRangePolicy | None = None
    log_scale: bool = False


class SeriesConfig(BaseModel):
    """v1 시리즈 스타일."""

    column: str
    label: str | None = None
    color: str | None = None
    line_width: float = 1.5
    marker_size: float | None = None
    opacity: float = 1.0


_V1_KIND = {
    ChartType.LINE: LayerKind.LINE,
    ChartType.BAR: LayerKind.BAR,
    ChartType.SCATTER: LayerKind.SCATTER,
    ChartType.HEATMAP: LayerKind.HEATMAP,
    ChartType.CANDLESTICK: LayerKind.CANDLESTICK,
}


class ChartSpec(BaseModel):
    dataset_id: str
    title: str | None = None
    x_axis: AxisConfig
    layers: list[LayerSpec] = Field(default_factory=list)
    axes: dict[AxisName, YAxisSpec] = Field(default_factory=dict)
    gaps: Literal["break"] = "break"  # 결측 구간에서 선을 끊는다 (P5)
    width: int = Field(default=1200, ge=64, le=8192)
    height: int = Field(default=600, ge=64, le=8192)
    format: ImageFormat = ImageFormat.PNG
    background_color: str = "#ffffff"
    grid: bool = True
    legend: bool = True
    downsample_algorithm: DownsampleAlgorithm = DownsampleAlgorithm.M4

    # --- v1 호환 필드 (layers가 비어 있을 때만 쓴다) ---
    chart_type: ChartType = ChartType.LINE
    y_axis: AxisConfig | None = None
    y_columns: list[str] = Field(default_factory=list)
    series: list[SeriesConfig] = Field(default_factory=list)
    # y범위 정책 (v1). v2에서는 axes.y.range_policy
    y_range_policy: YRangePolicy | None = None

    @model_validator(mode="after")
    def _adapt_v1(self) -> "ChartSpec":
        """v1 필드 → 레이어·축 (계획 문서 4.1의 어댑터)."""
        if not self.layers:
            self.layers = _layers_from_v1(self)
        if "y" not in self.axes and (self.y_axis is not None or self.y_range_policy is not None):
            self.axes["y"] = YAxisSpec(
                label=self.y_axis.label if self.y_axis else None,
                range=self.y_axis.range if self.y_axis else None,
                range_policy=self.y_range_policy,
                log_scale=self.y_axis.log_scale if self.y_axis else False,
            )
        return self

    def axis(self, name: AxisName) -> YAxisSpec:
        return self.axes.get(name) or YAxisSpec()

    @property
    def used_axes(self) -> list[AxisName]:
        """레이어가 하나라도 붙은 y축 (y, y2 순서)."""
        return [name for name in ("y", "y2") if any(layer.axis == name for layer in self.layers)]

    @property
    def data_columns(self) -> list[str]:
        """모든 레이어가 읽는 y 컬럼 (중복 없이, 처음 나온 순서)."""
        return list(dict.fromkeys(c for layer in self.layers for c in layer.data_columns))


def _layers_from_v1(spec: ChartSpec) -> list[LayerSpec]:
    kind = _V1_KIND[spec.chart_type]
    if spec.series:
        sources = [
            (
                s.column,
                LayerStyle(
                    color=s.color,
                    label=s.label or s.column,
                    line_width=s.line_width,
                    opacity=s.opacity,
                    marker_size=s.marker_size,
                ),
            )
            for s in spec.series
        ]
    elif spec.y_columns:
        sources = [(column, LayerStyle(label=column)) for column in spec.y_columns]
    elif spec.y_axis is not None:
        sources = [(spec.y_axis.column, LayerStyle())]
    else:
        sources = []
    return [LayerSpec(kind=kind, column=column, style=style) for column, style in sources]


class RenderResult(BaseModel):
    render_id: str
    format: ImageFormat
    width: int
    height: int
    size_bytes: int
    is_exact: bool = True  # 원본 전체를 그린 그림과 같음을 보증하는지 (P3)
    source: str = ""  # "raw" 또는 "pyramid:L{레벨}"
    point_count: int = 0
