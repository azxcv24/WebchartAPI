"""Tile-based renderer for dynamic interactive charts."""

import asyncio
import logging

import pandas as pd

from webchart.cache import redis_cache
from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.math_utils import visible_tile_indices
from webchart.models.chart import ChartSpec, ChartType
from webchart.models.dataset import DatasetMeta
from webchart.services.data.access import get_dataset_meta, read_window
from webchart.services.data.downsample import downsample
from webchart.services.data.pyramid import level_for_zoom, read_pyramid_window
from webchart.services.render.matplotlib_renderer import MatplotlibRenderer

logger = logging.getLogger("webchart.render.tile")

_renderer = MatplotlibRenderer()


def _resolve_x_range(meta: DatasetMeta, x_col: str) -> tuple[float, float] | None:
    info = next((c for c in meta.columns if c.name == x_col), None)
    if info and info.min_value is not None and info.max_value is not None:
        try:
            return float(info.min_value), float(info.max_value)
        except (TypeError, ValueError):
            return None
    return None


def _read_tile_data(
    dataset_id: str,
    meta: DatasetMeta,
    x_col: str,
    x_range: tuple[float, float],
    columns: list[str],
    zoom: int,
):
    """Read tile data from the pyramid when available, else from base.

    Runs in the render thread pool (sync). Picks a pyramid level for the
    zoom; falls back to base data when there's no pyramid for this x column
    or the zoom is finer than the pyramid was built for (where the base
    read is already cheap because the x range is narrow).
    """
    info = meta.pyramids.get(x_col)
    if info is not None:
        level = level_for_zoom(info, zoom)
        if level is not None:
            df = read_pyramid_window(dataset_id, x_col, level, x_range, columns)
            if df is not None:
                return df
    return read_window(dataset_id, x_col, x_range, columns)


async def render_tile(
    dataset_id: str,
    chart_spec: ChartSpec,
    zoom: int,
    tile_x: int,
    tile_y: int = 0,
    chart_hash: str = "",
    meta: DatasetMeta | None = None,
) -> bytes:
    """Render a single tile, using cache if available.

    `meta` may be passed in by callers that render multiple tiles for the
    same dataset so we avoid re-reading meta.json per tile.
    """
    key = redis_cache.tile_key(dataset_id, chart_hash, zoom, tile_x, tile_y)
    try:
        cached = await redis_cache.get_tile(key)
        if cached is not None:
            return cached
    except Exception:
        logger.debug("Redis unavailable, rendering without cache")

    if meta is None:
        meta = get_dataset_meta(dataset_id)
    x_col = chart_spec.x_axis.column

    x_bounds = _resolve_x_range(meta, x_col)
    if x_bounds is None:
        data_x_min, data_x_max = 0.0, 1.0
    else:
        data_x_min, data_x_max = x_bounds

    num_tiles = 2**zoom
    tile_width = (data_x_max - data_x_min) / num_tiles
    tile_x_min = data_x_min + tile_x * tile_width
    tile_x_max = tile_x_min + tile_width

    y_columns = _get_y_columns(chart_spec)
    columns = [x_col] + y_columns

    data = await run_in_thread(
        _read_tile_data,
        dataset_id, meta, x_col, (tile_x_min, tile_x_max), columns, zoom,
    )

    if not data.empty and len(data) > settings.tile_size:
        primary_y = y_columns[0] if y_columns else x_col
        data = await run_in_thread(
            downsample, data, x_col, primary_y, settings.tile_size, "lttb"
        )

    tile_bytes = await run_in_thread(
        _renderer.render_tile,
        data,
        x_col,
        y_columns,
        chart_spec.chart_type,
        settings.tile_size,
        (tile_x_min, tile_x_max),
    )

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

    x_bounds = _resolve_x_range(meta, x_col)
    data_x_min, data_x_max = x_bounds if x_bounds else viewport_x_range

    indices = visible_tile_indices(
        viewport_x_range[0], viewport_x_range[1],
        data_x_min, data_x_max,
        zoom,
    )

    num_tiles = 2**zoom
    all_indices = set(indices)
    for idx in indices:
        for offset in range(1, prefetch + 1):
            if idx - offset >= 0:
                all_indices.add(idx - offset)
            if idx + offset < num_tiles:
                all_indices.add(idx + offset)

    # render_tile awaits run_in_thread internally so the actual matplotlib
    # work fans out across the render pool — gather here gives concurrency.
    ordered = sorted(all_indices)
    tasks = [
        render_tile(dataset_id, chart_spec, zoom, idx, 0, chart_hash, meta=meta)
        for idx in ordered
    ]
    rendered = await asyncio.gather(*tasks, return_exceptions=True)

    result: dict[str, bytes] = {}
    for idx, outcome in zip(ordered, rendered):
        tile_id = f"{zoom}-{idx}-0"
        if isinstance(outcome, Exception):
            logger.warning(f"Tile {tile_id} render failed: {outcome}")
            continue
        result[tile_id] = outcome
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
