"""Unified data access layer using Dask for out-of-core processing."""

import logging
from pathlib import Path

import dask.dataframe as dd
import pandas as pd

from webchart.config import settings
from webchart.core.errors import DatasetNotFoundError, QueryTooLargeError
from webchart.models.dataset import DatasetMeta

logger = logging.getLogger("webchart.data.access")


def _dataset_dir(dataset_id: str) -> Path:
    return settings.data_dir / "datasets" / dataset_id


def get_dataset_meta(dataset_id: str) -> DatasetMeta:
    """Load dataset metadata from the sidecar JSON file."""
    meta_path = _dataset_dir(dataset_id) / "meta.json"
    if not meta_path.exists():
        raise DatasetNotFoundError(dataset_id)
    return DatasetMeta.model_validate_json(meta_path.read_text())


def read_full(dataset_id: str) -> dd.DataFrame:
    """Read the full dataset as a lazy Dask DataFrame."""
    data_dir = _dataset_dir(dataset_id) / "data"
    if not data_dir.exists():
        raise DatasetNotFoundError(dataset_id)
    return dd.read_parquet(data_dir)


def read_window(
    dataset_id: str,
    x_column: str,
    x_range: tuple[float, float] | None = None,
    columns: list[str] | None = None,
    max_rows: int | None = None,
) -> pd.DataFrame:
    """Read a windowed subset of the dataset, computed eagerly into a Pandas DataFrame.

    Args:
        dataset_id: Dataset identifier
        x_column: Column to filter on (typically the x-axis)
        x_range: (min, max) range filter on x_column
        columns: Specific columns to read (None = all)
        max_rows: Hard cap; defaults to settings.max_query_rows. Raises
            QueryTooLargeError if the filtered result would exceed it.
    """
    ddf = read_full(dataset_id)

    # Select columns
    if columns:
        # Always include x_column for filtering
        cols = list(set([x_column] + columns))
        cols = [c for c in cols if c in ddf.columns]
        ddf = ddf[cols]

    # Apply range filter
    if x_range is not None:
        x_min, x_max = x_range
        ddf = ddf[(ddf[x_column] >= x_min) & (ddf[x_column] <= x_max)]

    cap = max_rows if max_rows is not None else settings.max_query_rows

    # Cheap pre-check using partition counts before materializing
    try:
        approx_count = int(ddf.shape[0].compute())
    except Exception:
        approx_count = -1
    if 0 <= cap < approx_count:
        raise QueryTooLargeError(approx_count, cap)

    # Sort by x_column and compute
    result = ddf.compute()
    if len(result) > cap:
        raise QueryTooLargeError(len(result), cap)
    result = result.sort_values(x_column).reset_index(drop=True)

    return result


def read_sample(dataset_id: str, n: int = 100) -> pd.DataFrame:
    """Read the first n rows for preview. Cheap: only touches the head partition."""
    ddf = read_full(dataset_id)
    return ddf.head(n, compute=True)


def count_rows(dataset_id: str) -> int:
    """Fast row count from metadata."""
    meta = get_dataset_meta(dataset_id)
    return meta.row_count


def list_datasets(limit: int = 100, offset: int = 0) -> list[DatasetMeta]:
    """List available datasets with pagination (sorted by created_at desc)."""
    datasets_dir = settings.data_dir / "datasets"
    if not datasets_dir.exists():
        return []

    metas: list[DatasetMeta] = []
    for d in sorted(datasets_dir.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            metas.append(DatasetMeta.model_validate_json(meta_path.read_text()))
        except Exception:
            logger.warning(f"Failed to read metadata for {d.name}")

    metas.sort(key=lambda m: m.created_at, reverse=True)
    return metas[offset : offset + limit]


def delete_dataset(dataset_id: str) -> None:
    """Delete a dataset and its files."""
    import shutil

    ds_dir = _dataset_dir(dataset_id)
    if not ds_dir.exists():
        raise DatasetNotFoundError(dataset_id)
    shutil.rmtree(ds_dir)
