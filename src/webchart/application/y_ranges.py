"""y범위 정책 적용 (계획 문서 2.5, D1 해소).

한 화면(프레임)의 모든 타일은 여기서 정한 y범위 하나를 공유한다.
"""

from webchart.application.windows import WindowSet, fetch_view_windows
from webchart.domain.geometry import BucketGrid, XRange
from webchart.domain.y_range import YRange, YRangePolicy
from webchart.models.chart import ChartSpec
from webchart.models.dataset import DatasetMeta

# VIEWPORT 정책에서 y 최솟값·최댓값을 구할 때 쓰는 격자 폭 (버킷 수)
VIEWPORT_PROBE_BUCKETS = 512


def effective_policy(spec: ChartSpec, default: YRangePolicy) -> YRangePolicy:
    if spec.y_range_policy is not None:
        return spec.y_range_policy
    if spec.y_axis is not None and spec.y_axis.range is not None:
        return YRangePolicy.FIXED
    return default


def fixed_range(spec: ChartSpec) -> YRange:
    if spec.y_axis is None or spec.y_axis.range is None:
        raise ValueError("y범위 정책이 fixed인데 y_axis.range가 없다")
    return YRange(*spec.y_axis.range)


def global_range(meta: DatasetMeta, y_columns: list[str]) -> YRange | None:
    """데이터셋 전체의 y 최솟값·최댓값(인제스트 통계)으로 자동 범위. 통계가 없으면 None."""
    bounds = []
    for column in meta.columns:
        if column.name in y_columns and column.min_numeric is not None:
            assert column.max_numeric is not None
            bounds.append((column.min_numeric, column.max_numeric))
    if len(bounds) != len(set(y_columns)):
        return None
    return YRange.autoscaled(min(low for low, _ in bounds), max(high for _, high in bounds))


def range_from_windows(windows: WindowSet) -> YRange | None:
    bounds = windows.y_bounds()
    return YRange.autoscaled(*bounds) if bounds is not None else None


def resolve_frame_y_range(
    meta: DatasetMeta,
    spec: ChartSpec,
    x_column: str,
    y_columns: list[str],
    view: XRange,
) -> YRange:
    """타일 프레임 하나의 y범위. 기본 정책은 GLOBAL (캐시 효율이 가장 좋다).

    데이터가 없어 범위를 정할 수 없으면 (0, 1)을 쓴다.
    """
    policy = effective_policy(spec, YRangePolicy.GLOBAL)
    if policy is YRangePolicy.FIXED:
        return fixed_range(spec)
    if policy is YRangePolicy.GLOBAL:
        resolved = global_range(meta, y_columns)
        if resolved is not None:
            return resolved
    # VIEWPORT, 또는 통계가 없는 GLOBAL: 보이는 구간을 실제로 집계해 구한다
    probe = BucketGrid.covering(view, VIEWPORT_PROBE_BUCKETS)
    resolved = range_from_windows(fetch_view_windows(meta, x_column, y_columns, probe))
    if resolved is None:
        return YRange(0.0, 1.0)
    # 화면을 조금 움직여도 같은 범위가 나오게 해 타일 캐시를 다시 쓴다
    return resolved.snapped_outward() if policy is YRangePolicy.VIEWPORT else resolved
