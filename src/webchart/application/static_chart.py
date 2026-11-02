"""정적 렌더 유스케이스 (/charts/render, 계획 문서 1.7).

타일과 같은 파이프라인을 쓴다. 다른 점은 격자가 요청한 x범위·차트 크기에서 나온다는 것뿐이다.

1. plan: 차트의 데이터 영역 폭(px) = 버킷 수. x범위를 지정하지 않으면 x 전체 + 여백 5%
   (matplotlib 자동 스케일과 같다). y범위 기본 정책은 VIEWPORT (보이는 데이터에 맞춤)
2. fetch: application/windows.py, density.py가 원본·피라미드 중 고른다 (exact, source 결정)
3. project: 레이어 레지스트리 (application/layers.py)
4. paint: 축·제목·범례가 있는 완성 차트. 눈금은 서버 눈금 서비스 (application/axes.py)
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from webchart.application.axes import x_ticks, y_ticks
from webchart.application.density import fetch_view_density
from webchart.application.layers import LayerDrawing, project_layers
from webchart.application.windows import fetch_view_windows
from webchart.application.y_ranges import resolve_y_ranges
from webchart.domain.geometry import BucketGrid, XRange
from webchart.domain.projection import DensityGrid, Polyline
from webchart.domain.y_range import YRangePolicy, autoscale_bounds
from webchart.models.chart import DENSITY_LAYER_KINDS, ChartSpec, DownsampleAlgorithm
from webchart.models.dataset import ColumnInfo
from webchart.services.data.access import get_dataset_meta
from webchart.services.data.downsample import downsample
from webchart.services.render.matplotlib_renderer import AxisTicks, MatplotlibRenderer

_painter = MatplotlibRenderer()


@dataclass(frozen=True, slots=True)
class RenderedChart:
    image: bytes
    is_exact: bool
    source: str
    point_count: int  # 그린 점·막대·캔들 수 (밀도는 센 점 수)


def render_static_chart(spec: ChartSpec) -> RenderedChart:
    """spec 하나를 완성 차트 이미지로 그린다 (동기, 렌더 스레드에서 호출)."""
    meta = get_dataset_meta(spec.dataset_id)
    x_column = spec.x_axis.column
    x_info = next((c for c in meta.columns if c.name == x_column), None)

    width_px, height_px = _painter.plot_area_px(spec)
    x_view = _x_view(spec, x_info)
    grid = BucketGrid.covering(x_view, width_px)
    windows = fetch_view_windows(meta, x_column, spec.data_columns, grid)
    y_ranges = resolve_y_ranges(
        meta, spec, YRangePolicy.VIEWPORT, lambda: windows, dpr=1, snap_viewport=False
    )

    exact = [windows.is_exact]
    sources = [windows.source]
    densities: dict[int, DensityGrid] = {}
    for index, layer in enumerate(spec.layers):
        if layer.kind in DENSITY_LAYER_KINDS:
            result = fetch_view_density(
                meta, x_column, layer.data_columns[0], grid, y_ranges[layer.axis], height_px
            )
            densities[index] = result.density
            exact.append(result.is_exact)
            sources.append(result.source)

    drawings = project_layers(spec, windows, densities, dpr=1)
    if spec.downsample_algorithm != DownsampleAlgorithm.M4:
        drawings = _legacy_downsample(drawings, spec.downsample_algorithm, 2 * width_px)
        exact.append(False)

    ticks = AxisTicks(
        x=x_ticks(spec, x_info, x_view, width_px),
        y={name: y_ticks(y_range, height_px) for name, y_range in y_ranges.items()},
    )
    image = _painter.render_chart(spec, drawings, x_view, y_ranges, ticks)
    return RenderedChart(
        image=image,
        is_exact=all(exact),
        source="+".join(dict.fromkeys(sources)),
        point_count=sum(drawing.point_count for drawing in drawings),
    )


def _x_view(spec: ChartSpec, x_info: ColumnInfo | None) -> XRange:
    if spec.x_axis.range is not None:
        return XRange(*spec.x_axis.range)
    if x_info is None or x_info.min_numeric is None or x_info.max_numeric is None:
        return XRange(0.0, 1.0)
    return XRange(*autoscale_bounds(x_info.min_numeric, x_info.max_numeric))


def _legacy_downsample(
    drawings: list[LayerDrawing], algorithm: DownsampleAlgorithm, target_points: int
) -> list[LayerDrawing]:
    """이전 버전의 근사 알고리즘(LTTB 등)을 M4 꺾은선에 적용한다 (미리보기용, exact=false)."""
    reduced = []
    for drawing in drawings:
        line = drawing.shape
        if not isinstance(line, Polyline):
            reduced.append(drawing)
            continue
        keep = ~np.isnan(line.y)
        frame = pd.DataFrame({"x": line.x[keep], "y": line.y[keep]})
        frame = downsample(frame, "x", "y", target_points, algorithm.value)
        shape = Polyline(frame["x"].to_numpy(), frame["y"].to_numpy())
        reduced.append(LayerDrawing(drawing.layer, shape))
    return reduced
