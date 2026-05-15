"""Handle user interactions: hover, click, data lookup."""

import logging

import numpy as np
import pandas as pd

from webchart.core.concurrency import run_in_thread
from webchart.core.math_utils import Viewport
from webchart.models.session import SessionState, ViewportWindow
from webchart.models.ws_messages import HoverDataMessage
from webchart.services.data.access import read_window
from webchart.services.data.downsample import downsample

logger = logging.getLogger("webchart.session.interaction")


def _viewport_from_state(state: SessionState) -> Viewport:
    vp = state.viewport
    y_range = vp.y_range or (0.0, 1.0)
    return Viewport(
        pixel_width=vp.pixel_width,
        pixel_height=vp.pixel_height,
        x_min=vp.x_range[0],
        x_max=vp.x_range[1],
        y_min=y_range[0],
        y_max=y_range[1],
    )


async def resolve_hover(
    state: SessionState,
    pixel_x: float,
    pixel_y: float,
) -> HoverDataMessage | None:
    """Resolve a hover event to the nearest data point."""
    viewport = _viewport_from_state(state)
    data_x, data_y = viewport.pixel_to_data(pixel_x, pixel_y)

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
    x_range_width = viewport.x_max - viewport.x_min
    margin = x_range_width * 0.01  # 1% margin
    window = (data_x - margin, data_x + margin)

    try:
        data = await run_in_thread(
            read_window, state.dataset_id, x_col, window, [x_col, primary_y]
        )
    except Exception:
        return None

    if data.empty:
        return None

    # Find nearest point by x distance
    try:
        x_numeric = data[x_col].astype(float)
    except (TypeError, ValueError):
        # Non-numeric x (timestamp, string): use the raw column for comparison
        x_numeric = pd.to_datetime(data[x_col], errors="coerce").astype("int64", copy=False)
    distances = (x_numeric - data_x).abs()
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
