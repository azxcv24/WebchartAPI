"""인제스트 저장 규칙: x 정렬 + 작은 로우그룹 (계획 문서 2.7, D6 해소).

원본을 정렬 컬럼 오름차순으로, 일정한 행 수의 로우그룹으로 나눠 저장한다. 그러면
로우그룹마다 x 범위가 겹치지 않아, 범위 질의가 필요한 로우그룹만 읽을 수 있다
(infrastructure/raw_store.py).

정렬 비용: 수백 GB 원본을 정렬하려면 셔플이 필요하다. 시계열은 대부분 이미 정렬되어
들어오므로, 먼저 그대로 쓴 뒤 정렬 컬럼 하나만 읽어 정렬 여부를 검사하고, 정렬되지
않았을 때만 Dask로 정렬해 다시 쓴다.
"""

import logging
import shutil
from pathlib import Path

import dask.dataframe as dd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from webchart.config import settings
from webchart.core.errors import DatasetProcessingError
from webchart.infrastructure.raw_store import data_files
from webchart.services.data.x_domain import canonical_from_arrow

logger = logging.getLogger("webchart.ingest.sorted_layout")


def choose_sort_column(sample: pd.DataFrame, requested: str | None = None) -> str | None:
    """원본을 정렬할 컬럼을 고른다.

    우선순위: 요청한 컬럼 → 표본에서 이미 오름차순인 수치·시간 컬럼(인덱스 역할일 가능성이
    높다) → 첫 시간 컬럼 → 첫 수치 컬럼. 후보가 없으면 None (정렬하지 않는다).
    """
    if requested is not None:
        if requested not in sample.columns:
            raise DatasetProcessingError(f"정렬 컬럼 '{requested}'이 데이터에 없다")
        if not _is_axis_candidate(sample[requested]):
            raise DatasetProcessingError(f"정렬 컬럼 '{requested}'은 수치나 시간이어야 한다")
        return requested

    candidates = [str(name) for name in sample.columns if _is_axis_candidate(sample[name])]
    for name in candidates:
        values = sample[name].dropna()
        if len(values) > 1 and values.is_monotonic_increasing:
            return name
    for name in candidates:
        if pd.api.types.is_datetime64_any_dtype(sample[name]):
            return name
    return candidates[0] if candidates else None


def write_sorted(ddf: dd.DataFrame, data_dir: Path, sort_column: str | None) -> None:
    """ddf를 data_dir에 정렬된 Parquet으로 쓴다. 이미 있으면 교체한다."""
    staging = data_dir.with_name(f"{data_dir.name}.staging")
    for leftover in (staging, data_dir):
        if leftover.exists():
            shutil.rmtree(leftover)

    _write(ddf, staging)
    if sort_column is None or is_sorted_on_disk(staging, sort_column):
        staging.rename(data_dir)
        return

    logger.info(f"원본이 '{sort_column}' 기준으로 정렬되어 있지 않아 정렬한다: {data_dir}")
    _write(dd.read_parquet(staging).sort_values(sort_column), data_dir)
    shutil.rmtree(staging)


def is_sorted_on_disk(data_dir: Path, column: str) -> bool:
    """저장 순서대로 읽었을 때 column이 오름차순인지. 결측은 맨 뒤에만 있어야 한다.

    컬럼 하나만 파일 단위로 읽으므로 메모리 사용은 파일 하나의 컬럼 크기다.
    """
    previous_last = -np.inf
    seen_missing = False
    for path in data_files(data_dir):
        x = canonical_from_arrow(pq.read_table(path, columns=[column]).column(column))
        is_valid = ~np.isnan(x)
        if seen_missing and is_valid.any():
            return False
        if not is_valid.all():
            first_missing = int(np.argmin(is_valid))
            if is_valid[first_missing:].any():
                return False
            seen_missing = True
            x = x[:first_missing]
        if len(x) == 0:
            continue
        if x[0] < previous_last or np.any(np.diff(x) < 0):
            return False
        previous_last = float(x[-1])
    return True


def _write(ddf: dd.DataFrame, target: Path) -> None:
    ddf.to_parquet(
        target,
        engine="pyarrow",
        write_index=False,
        row_group_size=settings.ingest_row_group_rows,
    )


def _is_axis_candidate(values: pd.Series) -> bool:
    if pd.api.types.is_bool_dtype(values):
        return False
    return bool(
        pd.api.types.is_numeric_dtype(values) or pd.api.types.is_datetime64_any_dtype(values)
    )
