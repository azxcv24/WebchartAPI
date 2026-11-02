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
from webchart.models.dataset import ColumnKind, DatasetMeta, PyramidInfo
from webchart.services.data.access import get_dataset_meta, read_full

logger = logging.getLogger("webchart.data.pyramid")

# --- Paths -----------------------------------------------------------------

def pyramid_dir(dataset_id: str, x_column: str) -> Path:
    return settings.data_dir / "datasets" / dataset_id / "pyramid" / x_column


def level_path(dataset_id: str, x_column: str, level: int) -> Path:
    return pyramid_dir(dataset_id, x_column) / f"L{level}.parquet"


def has_pyramid(meta: DatasetMeta, x_column: str) -> bool:
    return x_column in meta.pyramids


def usable_pyramid(meta: DatasetMeta, x_column: str) -> PyramidInfo | None:
    """Return the pyramid for x_column only if it matches the current data.

    Guards against a stale pyramid after re-ingest: the build recorded the
    base row count and x bounds, and the renderer derives tile bounds from
    the current column stats. If those diverged, the pyramid buckets no
    longer line up, so we return None and the caller uses the base data.
    """
    info = meta.pyramids.get(x_column)
    if info is None:
        return None
    if meta.row_count and info.source_row_count and meta.row_count != info.source_row_count:
        return None
    col = next((c for c in meta.columns if c.name == x_column), None)
    if col is not None and col.min_numeric is not None and col.max_numeric is not None:
        if info.x_min is not None and info.x_max is not None:
            if col.min_numeric != info.x_min or col.max_numeric != info.x_max:
                return None
    return info


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
    """Read a pyramid level (small file), optionally filtered to x_range.

    Returns None when the level file is missing OR the windowed slice is
    empty, so the caller can fall back to the base data instead of drawing
    a blank tile. The x_range filter keeps one neighboring point on each
    side so drawn lines reach the tile edges.
    """
    path = level_path(dataset_id, x_column, level)
    if not path.exists():
        return None
    df = pd.read_parquet(path).sort_values(x_column).reset_index(drop=True)

    if x_range is not None:
        lo, hi = x_range
        inside = (df[x_column] >= lo) & (df[x_column] <= hi)
        idx = df.index[inside]
        if len(idx):
            # extend by one row on each side to avoid clipped edge lines
            start = max(0, int(idx[0]) - 1)
            stop = min(len(df), int(idx[-1]) + 2)
            df = df.iloc[start:stop]
        else:
            df = df.iloc[0:0]

    if columns:
        keep = [c for c in [x_column, *columns] if c in df.columns]
        df = df[list(dict.fromkeys(keep))]

    if df.empty:
        return None
    return df.reset_index(drop=True)


# --- Building --------------------------------------------------------------

def _minmax_select(
    pdf: pd.DataFrame,
    x_col: str,
    y_cols: list[str],
    carry: list[str],
    xmin: float,
    width: float,
    num_buckets: int,
) -> pd.DataFrame:
    """Per x-bucket, keep the real rows at the min and max of EACH y column.

    Taking the union over every y column preserves an envelope for all
    series (not just the primary), at the cost of up to 2*len(y_cols)
    points per bucket.
    """
    cols = [c for c in carry if c in pdf.columns]
    present_y = [c for c in y_cols if c in cols]
    sub = pdf[cols].dropna(subset=[x_col])
    if sub.empty or not present_y:
        return sub.reset_index(drop=True)

    x = sub[x_col].to_numpy(dtype="float64")
    bucket = np.floor((x - xmin) / width).astype("int64")
    np.clip(bucket, 0, num_buckets - 1, out=bucket)

    work = sub.assign(__b=bucket).reset_index(drop=True)
    idx = pd.Index([], dtype="int64")
    for yc in present_y:
        grp = work.dropna(subset=[yc]).groupby("__b")[yc]
        if grp.ngroups:
            idx = idx.union(pd.Index(grp.idxmin())).union(pd.Index(grp.idxmax()))
    out = work.loc[idx, cols].sort_values(x_col).reset_index(drop=True)
    return out


def _build_finest_level(
    ddf,
    x_col: str,
    y_cols: list[str],
    carry: list[str],
    xmin: float,
    width: float,
    num_buckets: int,
) -> pd.DataFrame:
    """Out-of-core finest level: per-partition local min-max, then combine."""
    meta = ddf._meta[[c for c in carry if c in ddf.columns]]
    candidates = ddf.map_partitions(
        _minmax_select,
        x_col, y_cols, carry, xmin, width, num_buckets,
        meta=meta,
    ).compute()
    candidates = candidates.reset_index(drop=True)
    # Second pass picks the global min/max row per bucket from the candidates.
    return _minmax_select(candidates, x_col, y_cols, carry, xmin, width, num_buckets)


def _choose_max_level(row_count: int, n_series: int = 1) -> int:
    """Deepest level that stays under pyramid_max_points and the data size.

    Each bucket holds up to 2*n_series points (min+max per series), so the
    finest level has ~2*n_series*2^L points; factor that into the cap.
    """
    per_bucket = max(1, 2 * n_series)
    bucket_cap = max(1, settings.pyramid_max_points // per_bucket)
    l_points = int(math.floor(math.log2(bucket_cap))) if bucket_cap >= 1 else 0
    l_data = int(math.floor(math.log2(max(1, row_count // 2)))) if row_count >= 2 else 0
    return max(0, min(settings.pyramid_max_level, l_points, l_data))


def _x_bounds(meta: DatasetMeta, ddf, x_column: str) -> tuple[float, float]:
    info = next((c for c in meta.columns if c.name == x_column), None)
    if info and info.min_numeric is not None and info.max_numeric is not None:
        return info.min_numeric, info.max_numeric
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

    numeric = {c.name for c in meta.columns if c.kind is ColumnKind.NUMERIC}
    if x_column not in numeric:
        # 시간 x축 피라미드는 Phase 1.3의 피라미드 v2에서 지원한다
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

    xmin, xmax = _x_bounds(meta, ddf, x_column)
    if not (xmax > xmin):
        raise DatasetProcessingError("degenerate x range (min >= max)")

    row_count = meta.row_count or int(ddf.shape[0].compute())
    max_level = _choose_max_level(row_count, len(y_cols))
    num_buckets = 2 ** max_level
    width = (xmax - xmin) / num_buckets

    ddf2 = ddf[carry].dropna(subset=[x_column])

    # Reset the pyramid dir for a clean rebuild.
    pdir = pyramid_dir(dataset_id, x_column)
    if pdir.exists():
        shutil.rmtree(pdir)
    pdir.mkdir(parents=True, exist_ok=True)

    finest = _build_finest_level(ddf2, x_column, y_cols, carry, xmin, width, num_buckets)
    _write_level(dataset_id, x_column, max_level, finest)

    # Coarser levels are re-bucketed from the level above (small, in memory).
    cur = finest
    for level in range(max_level - 1, -1, -1):
        width_l = (xmax - xmin) / (2 ** level)
        cur = _minmax_select(cur, x_column, y_cols, carry, xmin, width_l, 2 ** level)
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
        source_row_count=row_count,
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
