"""Handle user interactions: hover, click, data lookup."""

import logging

import pandas as pd

from webchart.core.concurrency import run_in_thread
from webchart.domain.geometry import ScreenTransform, XRange
from webchart.models.session import SessionState
from webchart.models.ws_messages import HoverDataMessage
from webchart.services.data.access import read_window

logger = logging.getLogger("webchart.session.interaction")


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


async def resolve_hover(
    state: SessionState,
    pixel_x: float,
    pixel_y: float,
) -> HoverDataMessage | None:
    """Resolve a hover event to the nearest data point."""
    transform = _screen_transform_of(state)
    if transform is None:
        return None
    data_x, _ = transform.to_data(pixel_x, pixel_y)

    spec = state.chart_config
    x_col = spec.x_axis.column

    # Get y columns
    y_columns = []
    if spec.series:
        y_columns = [s.column for s in spec.series]
    elif spec.y_columns:
        y_columns = spec.y_columns
    elif spec.y_axis:
        y_columns = [spec.y_axis.column]

    if not y_columns:
        return None

    primary_y = y_columns[0]

    # Read a small window around the hover point
    margin = transform.x_view.width * 0.01  # 뷰포트 폭의 1%
    window = (data_x - margin, data_x + margin)

    try:
        data = await run_in_thread(
            read_window, state.dataset_id, x_col, window, [x_col, primary_y]
        )
    except Exception:
        return None

    if data.empty:
        return None

    # Find nearest point by x distance. data_x is in the x column's native
    # units (read_window filtered the column against it directly).
    try:
        x_numeric = data[x_col].astype(float)
    except (TypeError, ValueError):
        # Non-numeric x (e.g. timestamp): compare in epoch-ns space. NaT/NaN
        # become NaN and are dropped so they cannot win idxmin.
        x_numeric = pd.to_datetime(data[x_col], errors="coerce").astype("int64", copy=False)
        x_numeric = x_numeric.where(data[x_col].notna())

    distances = (x_numeric - data_x).abs().dropna()
    if distances.empty:
        return None
    nearest_idx = distances.idxmin()
    nearest_row = data.loc[nearest_idx]

    raw_x = nearest_row[x_col]
    try:
        label_x = f"{float(raw_x):.4g}"
    except (TypeError, ValueError):
        label_x = str(raw_x)

    try:
        data_x_out = float(raw_x)
    except (TypeError, ValueError):
        data_x_out = float(x_numeric.loc[nearest_idx])

    return HoverDataMessage(
        data_x=data_x_out,
        data_y=float(nearest_row[primary_y]),
        series=primary_y,
        label=f"{x_col}={label_x}",
    )
