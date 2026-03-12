"""Data transformation utilities."""

import pandas as pd


def aggregate(
    df: pd.DataFrame,
    group_column: str,
    value_column: str,
    agg_func: str = "mean",
) -> pd.DataFrame:
    """Aggregate data by a grouping column."""
    valid_funcs = {"mean", "sum", "min", "max", "count", "median", "std"}
    if agg_func not in valid_funcs:
        raise ValueError(f"Invalid aggregation function: {agg_func}. Must be one of {valid_funcs}")

    return df.groupby(group_column, as_index=False).agg({value_column: agg_func})


def compute_bins(
    df: pd.DataFrame,
    column: str,
    num_bins: int = 50,
) -> pd.DataFrame:
    """Compute histogram bins for a numeric column."""
    values = df[column].dropna()
    counts, edges = pd.cut(values, bins=num_bins, retbins=True)
    hist = counts.value_counts().sort_index()

    return pd.DataFrame({
        "bin_start": edges[:-1],
        "bin_end": edges[1:],
        "count": hist.values,
    })
