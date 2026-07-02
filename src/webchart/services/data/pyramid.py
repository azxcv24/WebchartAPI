"""Multi-resolution pyramid: pre-aggregated overview levels for large data.

A chart at screen width W only needs ~2W points to look faithful. The cost
of a zoomed-out view of a huge dataset comes from scanning every partition.
This module pre-computes, per x column, a set of coarse-to-fine levels so a
zoomed-out render reads a small pre-aggregated file instead of the base data.

Level L divides the x range into 2^L buckets; for each bucket we keep the two
*real* rows holding the per-bucket min and max of the primary y column (a
min-max envelope), carrying all requested columns. This preserves the visual
shape and is built out-of-core via Dask map_partitions + a combine pass.

At render time:
  pyramid level = zoom + log2(points_per_tile)
so each tile still resolves to ~points_per_tile points. When the ideal level
exceeds what was built (high zoom / narrow x range), callers fall back to the
base data, where the read is already cheap.
"""

import logging
import math
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from webchart.config import settings
from webchart.core.errors import DatasetProcessingError
from webchart.models.dataset import DatasetMeta, PyramidInfo
from webchart.services.data.access import get_dataset_meta, read_full

logger = logging.getLogger("webchart.data.pyramid")

_NUMERIC_DTYPES = {
    "int8", "int16", "int32", "int64",
    "uint8", "uint16", "uint32", "uint64",
    "float16", "float32", "float64", "double",
}


# --- Paths -----------------------------------------------------------------

def pyramid_dir(dataset_id: str, x_column: str) -> Path:
    return settings.data_dir / "datasets" / dataset_id / "pyramid" / x_column


def level_path(dataset_id: str, x_column: str, level: int) -> Path:
    return pyramid_dir(dataset_id, x_column) / f"L{level}.parquet"


def has_pyramid(meta: DatasetMeta, x_column: str) -> bool:
    return x_column in meta.pyramids


# --- Level selection -------------------------------------------------------

def level_for_zoom(info: PyramidInfo, zoom: int) -> int | None:
    """Pyramid level for an interactive tile zoom, or None to use base data.

    A tile at zoom Z spans 1/2^Z of the x range. Level L has 2^L buckets
    (2 points each), so a tile covers 2^(L-Z) buckets = 2^(L-Z+1) points.
    Setting that to points_per_tile gives L = Z + log2(points_per_tile) - 1.
    """
    extra = round(math.log2(max(2, info.points_per_tile))) - 1
    ideal = zoom + extra
    if ideal > info.max_level:
        return None
    return max(0, ideal)


def level_for_width(info: PyramidInfo, width_px: int) -> int | None:
    """Pyramid level for a static render of the full x range at width_px."""
    ideal = math.ceil(math.log2(max(2, 2 * width_px)))
    if ideal > info.max_level:
        return None
    return max(0, ideal)


# --- Reading ---------------------------------------------------------------

def read_pyramid_window(
    dataset_id: str,
    x_column: str,
    level: int,
    x_range: tuple[float, float] | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame | None:
    """Read a pyramid level (small file), optionally filtered to x_range."""
    path = level_path(dataset_id, x_column, level)
    if not path.exists():
        return None
    df = pd.read_parquet(path)

    if x_range is not None:
        lo, hi = x_range
        df = df[(df[x_column] >= lo) & (df[x_column] <= hi)]

    if columns:
        keep = [c for c in [x_column, *columns] if c in df.columns]
        df = df[list(dict.fromkeys(keep))]

    return df.sort_values(x_column).reset_index(drop=True)


# --- Building --------------------------------------------------------------

def _minmax_select(
    pdf: pd.DataFrame,
    x_col: str,
    y_col: str,
    carry: list[str],
    xmin: float,
    width: float,
    num_buckets: int,
) -> pd.DataFrame:
    """Per x-bucket, keep the real rows at the min and max of y_col."""
    cols = [c for c in carry if c in pdf.columns]
    sub = pdf[cols].dropna(subset=[x_col, y_col])
    if sub.empty:
        return sub.reset_index(drop=True)

    x = sub[x_col].to_numpy(dtype="float64")
    bucket = np.floor((x - xmin) / width).astype("int64")
    np.clip(bucket, 0, num_buckets - 1, out=bucket)

    work = sub.assign(__b=bucket).reset_index(drop=True)
    grp = work.groupby("__b")[y_col]
    idx = pd.Index(grp.idxmin()).union(pd.Index(grp.idxmax()))
    out = work.loc[idx, cols].sort_values(x_col).reset_index(drop=True)
    return out


def _build_finest_level(
    ddf,
    x_col: str,
    y_col: str,
    carry: list[str],
    xmin: float,
    width: float,
    num_buckets: int,
) -> pd.DataFrame:
    """Out-of-core finest level: per-partition local min-max, then combine."""
    meta = ddf._meta[[c for c in carry if c in ddf.columns]]
    candidates = ddf.map_partitions(
        _minmax_select,
        x_col, y_col, carry, xmin, width, num_buckets,
        meta=meta,
    ).compute()
    candidates = candidates.reset_index(drop=True)
    # Second pass picks the global min/max row per bucket from the candidates.
    return _minmax_select(candidates, x_col, y_col, carry, xmin, width, num_buckets)


def _choose_max_level(row_count: int) -> int:
    """Deepest level that stays under pyramid_max_points and the data size."""
    bucket_cap = max(1, settings.pyramid_max_points // 2)
    l_points = int(math.floor(math.log2(bucket_cap))) if bucket_cap >= 1 else 0
    l_data = int(math.floor(math.log2(max(1, row_count // 2)))) if row_count >= 2 else 0
    return max(0, min(settings.pyramid_max_level, l_points, l_data))


def _x_bounds(meta: DatasetMeta, ddf, x_column: str) -> tuple[float, float]:
    info = next((c for c in meta.columns if c.name == x_column), None)
    if info and info.min_value is not None and info.max_value is not None:
        try:
            return float(info.min_value), float(info.max_value)
        except (TypeError, ValueError):
            pass
    lo, hi = ddf[x_column].min().compute(), ddf[x_column].max().compute()
    return float(lo), float(hi)


def _write_level(dataset_id: str, x_column: str, level: int, df: pd.DataFrame) -> None:
    df.to_parquet(level_path(dataset_id, x_column, level), index=False)


def build_pyramid(
    dataset_id: str,
    x_column: str,
    columns: list[str] | None = None,
    method: str = "minmax",
) -> PyramidInfo:
    """Build a min-max pyramid for x_column and record it in the dataset meta."""
    if method != "minmax":
        raise DatasetProcessingError(f"Unsupported pyramid method: {method}")

    meta = get_dataset_meta(dataset_id)
    ddf = read_full(dataset_id)
    if x_column not in ddf.columns:
        raise DatasetProcessingError(f"x_column '{x_column}' not in dataset")

    numeric = {
        c.name for c in meta.columns if c.dtype in _NUMERIC_DTYPES
    }
    if x_column not in numeric:
        raise DatasetProcessingError(
            f"x_column '{x_column}' must be numeric to build a pyramid"
        )

    if columns:
        carry = [x_column] + [c for c in columns if c != x_column]
    else:
        carry = [x_column] + [c for c in numeric if c != x_column]
    carry = [c for c in carry if c in ddf.columns]
    y_cols = [c for c in carry if c != x_column]
    if not y_cols:
        raise DatasetProcessingError("pyramid needs at least one numeric y column")
    primary = y_cols[0]

    xmin, xmax = _x_bounds(meta, ddf, x_column)
    if not (xmax > xmin):
        raise DatasetProcessingError("degenerate x range (min >= max)")

    row_count = meta.row_count or int(ddf.shape[0].compute())
    max_level = _choose_max_level(row_count)
    num_buckets = 2 ** max_level
    width = (xmax - xmin) / num_buckets

    ddf2 = ddf[carry].dropna(subset=[x_column, primary])

    # Reset the pyramid dir for a clean rebuild.
    pdir = pyramid_dir(dataset_id, x_column)
    if pdir.exists():
        shutil.rmtree(pdir)
    pdir.mkdir(parents=True, exist_ok=True)

    finest = _build_finest_level(ddf2, x_column, primary, carry, xmin, width, num_buckets)
    _write_level(dataset_id, x_column, max_level, finest)

    # Coarser levels are re-bucketed from the level above (small, in memory).
    cur = finest
    for level in range(max_level - 1, -1, -1):
        width_l = (xmax - xmin) / (2 ** level)
        cur = _minmax_select(cur, x_column, primary, carry, xmin, width_l, 2 ** level)
        _write_level(dataset_id, x_column, level, cur)

    info = PyramidInfo(
        x_column=x_column,
        columns=y_cols,
        max_level=max_level,
        points_per_tile=settings.pyramid_points_per_tile,
        method="minmax",
        total_points=int(len(finest)),
        x_min=xmin,
        x_max=xmax,
    )

    # Persist into meta.json (re-read to avoid clobbering concurrent updates).
    fresh = get_dataset_meta(dataset_id)
    fresh.pyramids[x_column] = info
    meta_path = settings.data_dir / "datasets" / dataset_id / "meta.json"
    meta_path.write_text(fresh.model_dump_json(indent=2))
    logger.info(
        f"Built pyramid for {dataset_id}/{x_column}: "
        f"levels 0..{max_level}, {len(finest)} finest points"
    )
    return info
