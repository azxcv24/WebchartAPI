"""Tile-based renderer for dynamic interactive charts."""

import logging

import pandas as pd

from webchart.cache import redis_cache
from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.math_utils import visible_tile_indices
from webchart.models.chart import ChartSpec, ChartType
from webchart.services.data.access import get_dataset_meta, read_window
from webchart.services.data.downsample import downsample
from webchart.services.render.matplotlib_renderer import MatplotlibRenderer

logger = logging.getLogger("webchart.render.tile")

_renderer = MatplotlibRenderer()


async def render_tile(
    dataset_id: str,
    chart_spec: ChartSpec,
    zoom: int,
    tile_x: int,
    tile_y: int = 0,
    chart_hash: str = "",
) -> bytes:
    """Render a single tile, using cache if available."""
    # Check cache
    key = redis_cache.tile_key(dataset_id, chart_hash, zoom, tile_x, tile_y)
    try:
        cached = await redis_cache.get_tile(key)
        if cached is not None:
            return cached
    except Exception:
        logger.debug("Redis unavailable, rendering without cache")

    # Compute tile data range
    meta = get_dataset_meta(dataset_id)
    x_col = chart_spec.x_axis.column

    # Find x range from metadata
    x_col_info = next((c for c in meta.columns if c.name == x_col), None)
    if x_col_info and x_col_info.min_value is not None and x_col_info.max_value is not None:
        data_x_min = float(x_col_info.min_value)
        data_x_max = float(x_col_info.max_value)
    else:
        # Fallback: need to compute from data
        data_x_min = 0.0
        data_x_max = 1.0

    num_tiles = 2**zoom
    tile_width = (data_x_max - data_x_min) / num_tiles
    tile_x_min = data_x_min + tile_x * tile_width
    tile_x_max = tile_x_min + tile_width

    # Read and downsample data for this tile
    y_columns = _get_y_columns(chart_spec)
    columns = [x_col] + y_columns

    data = read_window(dataset_id, x_col, (tile_x_min, tile_x_max), columns)

    if not data.empty and len(data) > settings.tile_size:
        primary_y = y_columns[0] if y_columns else x_col
        data = downsample(data, x_col, primary_y, settings.tile_size, "lttb")

    # Render tile in thread pool
    tile_bytes = await run_in_thread(
        _renderer.render_tile,
        data,
        x_col,
        y_columns,
        chart_spec.chart_type,
        settings.tile_size,
        (tile_x_min, tile_x_max),
    )

    # Cache result
    try:
        await redis_cache.set_tile(key, tile_bytes)
    except Exception:
        logger.debug("Failed to cache tile")

    return tile_bytes


async def render_visible_tiles(
    dataset_id: str,
    chart_spec: ChartSpec,
    viewport_x_range: tuple[float, float],
    zoom: int,
    chart_hash: str = "",
    prefetch: int = 2,
) -> dict[str, bytes]:
    """Render all tiles visible in the viewport, plus prefetch tiles.

    Returns dict of tile_id -> PNG bytes.
    """
    meta = get_dataset_meta(dataset_id)
    x_col = chart_spec.x_axis.column

    x_col_info = next((c for c in meta.columns if c.name == x_col), None)
    if x_col_info and x_col_info.min_value is not None and x_col_info.max_value is not None:
        data_x_min = float(x_col_info.min_value)
        data_x_max = float(x_col_info.max_value)
    else:
        data_x_min, data_x_max = viewport_x_range

    # Get visible tiles
    indices = visible_tile_indices(
        viewport_x_range[0], viewport_x_range[1],
        data_x_min, data_x_max,
        zoom,
    )

    # Add prefetch tiles
    num_tiles = 2**zoom
    all_indices = set(indices)
    for idx in indices:
        for offset in range(1, prefetch + 1):
            if idx - offset >= 0:
                all_indices.add(idx - offset)
            if idx + offset < num_tiles:
                all_indices.add(idx + offset)

    # Render all tiles
    result: dict[str, bytes] = {}
    for tile_idx in sorted(all_indices):
        tile_id = f"{zoom}-{tile_idx}-0"
        tile_bytes = await render_tile(
            dataset_id, chart_spec, zoom, tile_idx, 0, chart_hash,
        )
        result[tile_id] = tile_bytes

    return result


def _get_y_columns(spec: ChartSpec) -> list[str]:
    """Extract y column names from chart spec."""
    columns = []
    if spec.series:
        columns = [s.column for s in spec.series]
    elif spec.y_columns:
        columns = spec.y_columns
    elif spec.y_axis:
        columns = [spec.y_axis.column]
    return columns
