"""축 눈금 계산의 진입점: 데이터셋 메타(시간대) + 요청 → domain/ticks.py.

정적 렌더와 WS 프레임이 같은 함수를 쓴다 (계획 문서 2.5).

표시 시간대 규칙 (계획 문서 9장 결정)
1. 요청에 x_axis.timezone이 있으면 그 시간대
2. 없으면 컬럼의 원래 시간대 (tz-aware로 저장된 컬럼)
3. 시간대가 없는(naive) 컬럼은 저장된 벽시계 값 그대로
"""

from webchart.domain.geometry import XRange
from webchart.domain.ticks import Tick, numeric_ticks, place, target_tick_count, time_ticks
from webchart.domain.y_range import YRange
from webchart.models.chart import ChartSpec
from webchart.models.dataset import ColumnInfo, ColumnKind


def display_timezone(spec: ChartSpec, x_info: ColumnInfo | None) -> str | None:
    if spec.x_axis.timezone:
        return spec.x_axis.timezone
    if x_info is not None and x_info.timezone:
        return x_info.timezone
    return None


def x_ticks(
    spec: ChartSpec, x_info: ColumnInfo | None, view: XRange, length_px: float
) -> list[Tick]:
    count = target_tick_count(length_px)
    if x_info is not None and x_info.kind is ColumnKind.TIME:
        ticks = time_ticks(view.start, view.end, count, display_timezone(spec, x_info))
    else:
        ticks = numeric_ticks(view.start, view.end, count)
    return place(ticks, view.start, view.end, length_px)


def y_ticks(y_range: YRange, length_px: float) -> list[Tick]:
    """y 눈금. 픽셀은 아래쪽 끝(low)에서의 거리다."""
    ticks = numeric_ticks(y_range.low, y_range.high, target_tick_count(length_px, 60))
    return place(ticks, y_range.low, y_range.high, length_px)
