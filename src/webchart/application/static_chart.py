"""정적 렌더 유스케이스 (/charts/render, 계획 문서 1.7).

타일과 같은 파이프라인을 쓴다. 다른 점은 격자가 요청한 x범위·차트 크기에서 나온다는 것뿐이다.

1. plan: 차트의 데이터 영역 폭(px) = 버킷 수. x범위를 지정하지 않으면 x 전체 + 여백 5%
   (matplotlib 자동 스케일과 같다)
2. fetch: application/windows.py가 원본·피라미드 중 고른다 (exact, source 결정)
3. project: M4
4. paint: 축·제목·범례가 있는 완성 차트
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from webchart.application.windows import WindowSet, fetch_view_windows
from webchart.application.y_ranges import (
    effective_policy,
    fixed_range,
    global_range,
    range_from_windows,
)
from webchart.domain.geometry import BucketGrid, XRange
from webchart.domain.projection import Polyline, m4_polyline
from webchart.domain.y_range import YRange, YRangePolicy, autoscale_bounds
from webchart.models.chart import ChartSpec, DownsampleAlgorithm
from webchart.models.dataset import ColumnInfo, DatasetMeta
from webchart.services.data.access import get_dataset_meta
from webchart.services.data.downsample import downsample
from webchart.services.render.matplotlib_renderer import (
    MatplotlibRenderer,
    series_lines,
    spec_y_columns,
)

_painter = MatplotlibRenderer()


@dataclass(frozen=True, slots=True)
class RenderedChart:
    image: bytes
    is_exact: bool
    source: str
    point_count: int  # 실제로 그린 점 수 (선 끊기 구분자 제외)


def render_static_chart(spec: ChartSpec) -> RenderedChart:
    """spec 하나를 완성 차트 이미지로 그린다 (동기, 렌더 스레드에서 호출)."""
    meta = get_dataset_meta(spec.dataset_id)
    x_column = spec.x_axis.column
    y_columns = spec_y_columns(spec)
    x_info = next((c for c in meta.columns if c.name == x_column), None)

    width_px, _ = _painter.plot_area_px(spec)
    x_view = _x_view(spec, x_info)
    windows = fetch_view_windows(meta, x_column, y_columns, BucketGrid.covering(x_view, width_px))
    y_range = _y_range(spec, meta, y_columns, windows)

    polylines = {series: m4_polyline(window) for series, window in windows.windows.items()}
    is_exact = windows.is_exact
    if spec.downsample_algorithm != DownsampleAlgorithm.M4:
        polylines = _legacy_downsample(polylines, spec.downsample_algorithm, 2 * width_px)
        is_exact = False

    image = _painter.render_chart(spec, series_lines(spec, polylines), x_view, y_range, x_info)
    return RenderedChart(
        image=image,
        is_exact=is_exact and spec.chart_type.value == "line",
        source=windows.source,
        point_count=sum(line.point_count for line in polylines.values()),
    )


def _x_view(spec: ChartSpec, x_info: ColumnInfo | None) -> XRange:
    if spec.x_axis.range is not None:
        return XRange(*spec.x_axis.range)
    if x_info is None or x_info.min_numeric is None or x_info.max_numeric is None:
        return XRange(0.0, 1.0)
    return XRange(*autoscale_bounds(x_info.min_numeric, x_info.max_numeric))


def _y_range(
    spec: ChartSpec, meta: DatasetMeta, y_columns: list[str], windows: WindowSet
) -> YRange | None:
    policy = effective_policy(spec, YRangePolicy.VIEWPORT)
    if policy is YRangePolicy.FIXED:
        return fixed_range(spec)
    if policy is YRangePolicy.GLOBAL:
        resolved = global_range(meta, y_columns)
        if resolved is not None:
            return resolved
    return range_from_windows(windows)


def _legacy_downsample(
    polylines: dict[str, Polyline], algorithm: DownsampleAlgorithm, target_points: int
) -> dict[str, Polyline]:
    """이전 버전의 근사 알고리즘(LTTB 등)을 M4 결과에 적용한다 (미리보기용, exact=false)."""
    reduced: dict[str, Polyline] = {}
    for series, line in polylines.items():
        keep = ~np.isnan(line.y)
        frame = pd.DataFrame({"x": line.x[keep], "y": line.y[keep]})
        frame = downsample(frame, "x", "y", target_points, algorithm.value)
        reduced[series] = Polyline(frame["x"].to_numpy(), frame["y"].to_numpy())
    return reduced
