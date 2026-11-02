"""사용자 인터랙션 처리: 호버 위치의 최근접 데이터 점 찾기."""

import logging

import numpy as np

from webchart.core.concurrency import run_in_thread
from webchart.domain.geometry import ScreenTransform, XRange
from webchart.models.chart import ChartSpec
from webchart.models.session import SessionState
from webchart.models.ws_messages import HoverDataMessage
from webchart.services.data.access import get_dataset_meta, read_window
from webchart.services.data.x_domain import format_x, to_canonical

logger = logging.getLogger("webchart.session.interaction")

# 호버 지점 좌우로 원본을 읽을 폭 (뷰포트 x 폭 대비 비율)
HOVER_SEARCH_RATIO = 0.01


def _screen_transform_of(state: SessionState) -> ScreenTransform | None:
    """세션 뷰포트의 화면 변환. 뷰포트가 비정상(빈 구간 등)이면 None.

    y_range가 없으면 (0, 1)을 가정한다. 실제 타일의 y 스케일과 다를 수 있다
    (D8, Phase 3에서 서버가 확정한 y범위를 세션에 저장하도록 바꾼다).
    """
    viewport = state.viewport
    y_low, y_high = viewport.y_range or (0.0, 1.0)
    try:
        return ScreenTransform(
            x_view=XRange(*viewport.x_range),
            y_low=y_low,
            y_high=y_high,
            width_px=viewport.pixel_width,
            height_px=viewport.pixel_height,
        )
    except ValueError:
        return None


def _y_columns_of(spec: ChartSpec) -> list[str]:
    if spec.series:
        return [series.column for series in spec.series]
    if spec.y_columns:
        return spec.y_columns
    if spec.y_axis:
        return [spec.y_axis.column]
    return []


async def resolve_hover(
    state: SessionState,
    pixel_x: float,
    pixel_y: float,
) -> HoverDataMessage | None:
    """호버 픽셀 위치에서 x가 가장 가까운 **원본** 데이터 점을 찾는다.

    값은 집계 결과가 아니라 원본 행에서 읽는다 (계획 문서 P4). 지금은 첫 시리즈만
    반환한다 (D8, Phase 3.2에서 모든 시리즈로 확장). 찾지 못하면 None.
    """
    transform = _screen_transform_of(state)
    y_columns = _y_columns_of(state.chart_config)
    if transform is None or not y_columns:
        return None

    hover_x, _ = transform.to_data(pixel_x, pixel_y)
    x_column = state.chart_config.x_axis.column
    primary_y = y_columns[0]
    margin = transform.x_view.width * HOVER_SEARCH_RATIO

    try:
        meta = get_dataset_meta(state.dataset_id)
        rows = await run_in_thread(
            read_window,
            state.dataset_id,
            x_column,
            (hover_x - margin, hover_x + margin),
            [x_column, primary_y],
            meta=meta,
        )
    except Exception as error:
        logger.debug(f"호버 데이터 읽기 실패: {error}")
        return None

    x_values = to_canonical(rows[x_column]) if not rows.empty else np.empty(0)
    if np.isnan(x_values).all():  # 빈 결과이거나 x가 모두 결측
        return None
    nearest = int(np.nanargmin(np.abs(x_values - hover_x)))
    nearest_x = float(x_values[nearest])
    x_info = next((c for c in meta.columns if c.name == x_column), None)

    return HoverDataMessage(
        data_x=nearest_x,
        data_y=float(rows[primary_y].iloc[nearest]),
        series=primary_y,
        label=f"{x_column}={format_x(nearest_x, x_info)}",
    )
