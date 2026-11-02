"""y범위 정책 적용 (계획 문서 2.5, D1 해소).

축(y, y2)마다 y범위를 하나 정하고, 한 화면(프레임)의 모든 타일이 그 값을 공유한다.

정책
- FIXED: 사용자가 지정한 범위
- GLOBAL: 데이터셋 전체 y 최솟값·최댓값(인제스트 통계). 줌·이동에도 변하지 않는다.
  단, bar 레이어가 있는 축은 막대 값(sum 등)이 줌에 따라 달라 통계로 알 수 없으므로
  VIEWPORT로 계산한다.
- VIEWPORT: 보이는 구간을 실제로 집계·투영한 값의 범위
"""

from collections.abc import Callable

from webchart.application.layers import axis_value_bounds
from webchart.application.windows import WindowSet
from webchart.domain.y_range import YRange, YRangePolicy
from webchart.models.chart import AxisName, ChartSpec, LayerKind, YAxisSpec
from webchart.models.dataset import DatasetMeta

# 데이터가 없어 범위를 정할 수 없을 때
EMPTY_RANGE = YRange(0.0, 1.0)


def effective_policy(axis: YAxisSpec, default: YRangePolicy) -> YRangePolicy:
    if axis.range_policy is not None:
        return axis.range_policy
    if axis.range is not None:
        return YRangePolicy.FIXED
    return default


def resolve_y_ranges(
    meta: DatasetMeta,
    spec: ChartSpec,
    default: YRangePolicy,
    probe: Callable[[], WindowSet],
    dpr: int,
    snap_viewport: bool,
) -> dict[AxisName, YRange]:
    """레이어가 붙은 축마다 y범위를 정한다.

    Args:
        probe: 보이는 구간의 집계 창을 만드는 함수. VIEWPORT가 필요할 때만 한 번 부른다.
        snap_viewport: VIEWPORT 범위를 보기 좋은 값으로 넓힐지 (타일 캐시 재사용용).
            정적 렌더는 matplotlib 자동 스케일과 같게 넓히지 않는다.
    """
    probed: list[WindowSet] = []

    def windows() -> WindowSet:
        if not probed:
            probed.append(probe())
        return probed[0]

    ranges: dict[AxisName, YRange] = {}
    for name in spec.used_axes:
        axis = spec.axis(name)
        policy = effective_policy(axis, default)
        if policy is YRangePolicy.FIXED:
            if axis.range is None:
                raise ValueError(f"{name}축 정책이 fixed인데 range가 없다")
            ranges[name] = YRange(*axis.range)
            continue
        if policy is YRangePolicy.GLOBAL and not _has_bars(spec, name):
            resolved = global_range(meta, spec, name)
            if resolved is not None:
                ranges[name] = resolved
                continue
        bounds = axis_value_bounds(spec, name, windows(), dpr)
        if bounds is None:
            ranges[name] = EMPTY_RANGE
            continue
        resolved = YRange.autoscaled(*bounds)
        is_viewport = policy is YRangePolicy.VIEWPORT
        ranges[name] = resolved.snapped_outward() if is_viewport and snap_viewport else resolved
    return ranges


def global_range(meta: DatasetMeta, spec: ChartSpec, axis: AxisName) -> YRange | None:
    """축 레이어들의 컬럼 통계로 자동 범위. 통계가 없는 컬럼이 있으면 None."""
    stats = {column.name: column for column in meta.columns}
    lows, highs = [], []
    for layer in spec.layers:
        if layer.axis != axis:
            continue
        for name in layer.data_columns:
            column = stats.get(name)
            if column is None or column.min_numeric is None or column.max_numeric is None:
                return None
            lows.append(column.min_numeric)
            highs.append(column.max_numeric)
        if layer.kind == LayerKind.AREA:
            lows.append(0.0)
            highs.append(0.0)
    if not lows:
        return None
    return YRange.autoscaled(min(lows), max(highs))


def _has_bars(spec: ChartSpec, axis: AxisName) -> bool:
    return any(layer.axis == axis and layer.kind == LayerKind.BAR for layer in spec.layers)
