"""Unified data access layer using Dask for out-of-core processing."""

import logging
from pathlib import Path

import dask.dataframe as dd
import pandas as pd

from webchart.config import settings
from webchart.core.errors import DatasetNotFoundError, QueryTooLargeError
from webchart.models.dataset import ColumnInfo, DatasetMeta
from webchart.services.data.x_domain import to_native_bound

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
    meta: DatasetMeta | None = None,
) -> pd.DataFrame:
    """x 범위에 속한 행을 읽어 x 오름차순 pandas DataFrame으로 반환한다.

    Args:
        dataset_id: 데이터셋 ID
        x_column: 범위 필터를 걸 컬럼 (보통 x축)
        x_range: 정규 좌표(x_domain.py)로 표현한 [low, high] 닫힌 범위. 시간 컬럼이면
            epoch 나노초이며, 여기서 컬럼의 원래 타입으로 바꿔 비교한다.
        columns: 읽을 컬럼 (None이면 전체)
        max_rows: 결과 행 수 상한. 기본값은 settings.max_query_rows이며 넘으면
            QueryTooLargeError를 던진다.
        meta: 이미 읽어 둔 메타데이터. 없으면 x 범위 변환에 필요할 때 읽는다.
    """
    ddf = read_full(dataset_id)

    # Select columns
    if columns:
        # Always include x_column for filtering
        cols = list(set([x_column] + columns))
        cols = [c for c in cols if c in ddf.columns]
        ddf = ddf[cols]

    if x_range is not None:
        column = _column_info(meta or get_dataset_meta(dataset_id), x_column)
        low, high = (to_native_bound(bound, column) for bound in x_range)
        ddf = ddf[(ddf[x_column] >= low) & (ddf[x_column] <= high)]

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


def _column_info(meta: DatasetMeta, name: str) -> ColumnInfo | None:
    return next((column for column in meta.columns if column.name == name), None)


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
