"""데이터셋 접근: 메타데이터 저장소와 원본 범위 읽기의 진입점.

범위 읽기는 infrastructure/raw_store.py(zone map + pyarrow)로, 전체를 훑는 작업
(미리보기 등)은 Dask로 한다.
"""

import logging
from pathlib import Path

import dask.dataframe as dd
import pandas as pd

from webchart.config import settings
from webchart.core.errors import DatasetNotFoundError
from webchart.infrastructure.raw_store import RawStore, forget_dataset, raw_store_for
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


def save_dataset_meta(meta: DatasetMeta) -> None:
    """meta.json을 원자적으로 교체한다 (쓰는 도중 다른 스레드가 반쯤 쓴 파일을 읽지 않게)."""
    meta_path = _dataset_dir(meta.dataset_id) / "meta.json"
    staging = meta_path.with_suffix(".json.tmp")
    staging.write_text(meta.model_dump_json(indent=2))
    staging.replace(meta_path)


def read_full(dataset_id: str) -> dd.DataFrame:
    """Read the full dataset as a lazy Dask DataFrame."""
    data_dir = _dataset_dir(dataset_id) / "data"
    if not data_dir.exists():
        raise DatasetNotFoundError(dataset_id)
    return dd.read_parquet(data_dir)


def raw_store(meta: DatasetMeta) -> RawStore:
    """데이터셋의 원본 저장소 (data_version별로 재사용된다)."""
    # 실시간 추가는 파일을 늘리므로 세대(generation)가 바뀌면 zone map을 새로 만든다
    version = f"{meta.data_version}:{meta.generation}"
    return raw_store_for(_dataset_dir(meta.dataset_id) / "data", meta.dataset_id, version)


def read_sample(dataset_id: str, n: int = 100) -> pd.DataFrame:
    """Read the first n rows for preview. Cheap: only touches the head partition."""
    ddf = read_full(dataset_id)
    return ddf.head(n, compute=True)


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
    forget_dataset(dataset_id)
    shutil.rmtree(ds_dir)
