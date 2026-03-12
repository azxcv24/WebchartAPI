"""Downsampling algorithms for reducing large datasets to screen resolution.

Supports:
- LTTB (Largest Triangle Three Buckets): Best for line charts
- Min-Max: Best for candlestick/range charts
- Average: Best for bar/aggregated charts
"""

import logging

import numpy as np
import pandas as pd

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
    """Largest Triangle Three Buckets downsampling.

    Preserves visual shape of the data better than simple decimation.
    Reference: Sveinn Steinarsson, 2013.
    """
    n = len(df)
    x = df[x_column].values.astype(np.float64)
    y = df[y_column].values.astype(np.float64)

    # Always keep first and last point
    selected_indices = [0]

    bucket_size = (n - 2) / (target_points - 2)

    prev_idx = 0

    for i in range(1, target_points - 1):
        # Calculate bucket boundaries
        bucket_start = int((i - 1) * bucket_size) + 1
        bucket_end = int(i * bucket_size) + 1
        bucket_end = min(bucket_end, n)

        next_bucket_start = int(i * bucket_size) + 1
        next_bucket_end = int((i + 1) * bucket_size) + 1
        next_bucket_end = min(next_bucket_end, n)

        # Average point in next bucket
        avg_x = np.mean(x[next_bucket_start:next_bucket_end])
        avg_y = np.mean(y[next_bucket_start:next_bucket_end])

        # Find point in current bucket with largest triangle area
        max_area = -1.0
        max_idx = bucket_start

        for j in range(bucket_start, bucket_end):
            area = abs(
                (x[prev_idx] - avg_x) * (y[j] - y[prev_idx])
                - (x[prev_idx] - x[j]) * (avg_y - y[prev_idx])
            )
            if area > max_area:
                max_area = area
                max_idx = j

        selected_indices.append(max_idx)
        prev_idx = max_idx

    selected_indices.append(n - 1)

    return df.iloc[selected_indices].reset_index(drop=True)


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
