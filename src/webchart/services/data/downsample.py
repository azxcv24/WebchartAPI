"""Downsampling algorithms for reducing large datasets to screen resolution.

Supports:
- LTTB (Largest Triangle Three Buckets): Best for line charts
- Min-Max: Best for candlestick/range charts
- Average: Best for bar/aggregated charts
"""

import logging

import numpy as np
import pandas as pd

from webchart.services.data.x_domain import to_canonical

logger = logging.getLogger("webchart.data.downsample")


def downsample(
    df: pd.DataFrame,
    x_column: str,
    y_column: str,
    target_points: int,
    algorithm: str = "lttb",
) -> pd.DataFrame:
    """Downsample a DataFrame to target_points rows.

    Args:
        df: Input DataFrame, must be sorted by x_column
        x_column: Name of the x-axis column
        y_column: Name of the y-axis column
        target_points: Target number of output points
        algorithm: "lttb", "minmax", or "average"
    """
    n = len(df)
    if n <= target_points:
        return df

    if algorithm == "lttb":
        return _lttb(df, x_column, y_column, target_points)
    elif algorithm == "minmax":
        return _minmax(df, x_column, y_column, target_points)
    elif algorithm == "average":
        return _average(df, x_column, y_column, target_points)
    else:
        logger.warning(f"Unknown algorithm '{algorithm}', falling back to LTTB")
        return _lttb(df, x_column, y_column, target_points)


def _lttb(
    df: pd.DataFrame,
    x_column: str,
    y_column: str,
    target_points: int,
) -> pd.DataFrame:
    """Largest Triangle Three Buckets downsampling (vectorized).

    Preserves visual shape better than simple decimation.
    Reference: Sveinn Steinarsson, 2013.

    The bucket selection has a sequential dependency (the previously picked
    point feeds the next triangle), so the outer loop remains in Python —
    but the per-bucket triangle-area search is vectorized with NumPy.
    """
    n = len(df)
    if target_points < 3:
        # Degenerate: just return endpoints (and one middle if possible)
        if n <= target_points:
            return df.copy()
        idx = list({0, n // 2, n - 1})[:target_points]
        return df.iloc[sorted(idx)].reset_index(drop=True)

    x = to_canonical(df[x_column])  # 시간 x축도 삼각형 면적을 계산할 수 있게 수치로 바꾼다
    y = df[y_column].to_numpy(dtype=np.float64, copy=False)

    bucket_size = (n - 2) / (target_points - 2)

    # Bucket edges of length target_points so that bucket k spans [E[k], E[k+1]).
    # E[0] = 1 (skip the kept first point); E[-1] = n (cap to array length).
    edges = (np.arange(target_points) * bucket_size).astype(np.int64) + 1
    edges = np.minimum(edges, n)
    edges[0] = 1
    edges[-1] = n

    selected = np.empty(target_points, dtype=np.int64)
    selected[0] = 0
    selected[-1] = n - 1

    prev_idx = 0
    for k in range(target_points - 2):
        bs, be = int(edges[k]), int(edges[k + 1])
        nbs, nbe = int(edges[k + 1]), int(edges[k + 2])

        if be <= bs:
            # Empty bucket — repeat previous index
            selected[k + 1] = prev_idx
            continue

        if nbe > nbs:
            avg_x = x[nbs:nbe].mean()
            avg_y = y[nbs:nbe].mean()
        else:
            avg_x = x[-1]
            avg_y = y[-1]

        # Vectorized triangle-area over current bucket
        cx = x[bs:be]
        cy = y[bs:be]
        area = np.abs(
            (x[prev_idx] - avg_x) * (cy - y[prev_idx])
            - (x[prev_idx] - cx) * (avg_y - y[prev_idx])
        )
        max_offset = int(np.argmax(area))
        chosen = bs + max_offset
        selected[k + 1] = chosen
        prev_idx = chosen

    return df.iloc[selected].reset_index(drop=True)


def _minmax(
    df: pd.DataFrame,
    x_column: str,
    y_column: str,
    target_points: int,
) -> pd.DataFrame:
    """Min-Max bucketing - keeps min and max per bucket.

    Good for candlestick charts and showing data range.
    Each bucket produces 2 points (min and max), so target_points is halved.
    """
    n = len(df)
    num_buckets = target_points // 2
    bucket_size = n / num_buckets

    selected_indices = []

    for i in range(num_buckets):
        start = int(i * bucket_size)
        end = int((i + 1) * bucket_size)
        end = min(end, n)

        if start >= end:
            continue

        bucket = df.iloc[start:end]
        y_vals = bucket[y_column]

        min_idx = y_vals.idxmin()
        max_idx = y_vals.idxmax()

        # Add in order of x position
        if min_idx <= max_idx:
            selected_indices.extend([min_idx, max_idx])
        else:
            selected_indices.extend([max_idx, min_idx])

    # Deduplicate while preserving order
    seen = set()
    unique_indices = []
    for idx in selected_indices:
        if idx not in seen:
            seen.add(idx)
            unique_indices.append(idx)

    return df.loc[unique_indices].reset_index(drop=True)


def _average(
    df: pd.DataFrame,
    x_column: str,
    y_column: str,
    target_points: int,
) -> pd.DataFrame:
    """Average bucketing - compute mean per bucket.

    Good for bar charts and aggregated views.
    """
    n = len(df)
    bucket_indices = np.linspace(0, n, target_points + 1, dtype=int)

    rows = []
    for i in range(target_points):
        start = bucket_indices[i]
        end = bucket_indices[i + 1]
        if start >= end:
            continue

        bucket = df.iloc[start:end]
        row = {x_column: bucket[x_column].mean()}

        # Average all numeric columns
        for col in df.columns:
            if col != x_column:
                if pd.api.types.is_numeric_dtype(df[col]):
                    row[col] = bucket[col].mean()
                else:
                    row[col] = bucket[col].iloc[len(bucket) // 2]  # median position

        rows.append(row)

    return pd.DataFrame(rows)


def downsample_multi_series(
    df: pd.DataFrame,
    x_column: str,
    y_columns: list[str],
    target_points: int,
    algorithm: str = "lttb",
) -> pd.DataFrame:
    """Downsample with multiple y-columns.

    For LTTB, uses the first y_column to determine point selection,
    then includes all other columns at the selected indices.
    For minmax and average, operates on each y_column independently
    and merges results.
    """
    if algorithm == "lttb":
        primary_y = y_columns[0]
        result = _lttb(df, x_column, primary_y, target_points)
        return result

    # For minmax/average, use all columns together
    return downsample(df, x_column, y_columns[0], target_points, algorithm)
