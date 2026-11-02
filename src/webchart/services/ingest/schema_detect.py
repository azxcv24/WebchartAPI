"""Parquet 메타데이터에서 컬럼 타입·분류·통계를 읽는다 (데이터 본문은 읽지 않는다)."""

import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from webchart.models.dataset import ColumnInfo, ColumnKind
from webchart.services.data.x_domain import canonical_scalar, column_kind_of

logger = logging.getLogger("webchart.ingest.schema_detect")


def detect_schema(parquet_dir: Path) -> list[ColumnInfo]:
    """데이터셋의 모든 컬럼에 대해 ColumnInfo를 만든다."""
    pf = pq.ParquetDataset(parquet_dir)
    schema = pf.schema

    columns: list[ColumnInfo] = []

    for i in range(len(schema)):
        field = schema.field(i)
        dtype_str = _arrow_type_to_str(field.type)

        col_info = ColumnInfo(
            name=field.name,
            dtype=dtype_str,
            kind=column_kind_of(field.type),
            nullable=field.nullable,
            timezone=field.type.tz if pa.types.is_timestamp(field.type) else None,
        )

        # 로우그룹 통계가 없거나 읽을 수 없는 컬럼은 통계 없이 둔다
        try:
            _fill_statistics(col_info, parquet_dir, field.name)
        except Exception:
            logger.debug(f"Could not read statistics for column {field.name}")

        columns.append(col_info)

    return columns


def _fill_statistics(col_info: ColumnInfo, parquet_dir: Path, col_name: str) -> None:
    """로우그룹 통계를 모아 min/max/null_count를 채운다."""
    parquet_files = list(parquet_dir.glob("*.parquet"))
    if not parquet_files:
        return

    total_nulls = 0
    global_min = None
    global_max = None

    for pf_path in parquet_files:
        pf = pq.ParquetFile(pf_path)
        meta = pf.metadata

        for rg_idx in range(meta.num_row_groups):
            rg = meta.row_group(rg_idx)
            col_idx = _find_column_index(meta, col_name)
            if col_idx is None:
                continue

            col_meta = rg.column(col_idx)
            if col_meta.is_stats_set:
                stats = col_meta.statistics
                total_nulls += stats.null_count

                if stats.has_min_max:
                    if global_min is None or stats.min < global_min:
                        global_min = stats.min
                    if global_max is None or stats.max > global_max:
                        global_max = stats.max

    col_info.null_count = total_nulls
    _assign_bounds(col_info, global_min, global_max)


def _assign_bounds(col_info: ColumnInfo, low: Any, high: Any) -> None:
    """원시 통계값을 표시값(min_value)과 정규 좌표(min_numeric)로 나눠 기록한다."""
    if low is None or high is None:
        return
    if col_info.kind is ColumnKind.NUMERIC:
        col_info.min_value, col_info.max_value = float(low), float(high)
    elif col_info.kind is ColumnKind.TIME:
        col_info.min_value = pd.Timestamp(low).isoformat()
        col_info.max_value = pd.Timestamp(high).isoformat()
    elif isinstance(low, str) and isinstance(high, str):
        col_info.min_value, col_info.max_value = low, high
    col_info.min_numeric = canonical_scalar(low, col_info.kind)
    col_info.max_numeric = canonical_scalar(high, col_info.kind)


def _find_column_index(meta: pq.FileMetaData, col_name: str) -> int | None:
    """Find column index in Parquet physical schema by top-level field name.

    Parquet's physical schema is the leaf-flattened view (nested struct
    fields appear as `parent.child`). Match either the full path or the
    top-level component so callers can pass Arrow field names directly.
    """
    schema = meta.schema
    for i in range(len(schema)):
        col = schema.column(i)
        path = col.path  # e.g. "a.b" for nested
        if path == col_name or path.split(".")[0] == col_name:
            return i
    return None


def _arrow_type_to_str(arrow_type: object) -> str:
    """Convert Arrow type to a human-readable string."""
    import pyarrow as pa

    type_map = {
        pa.int8(): "int8",
        pa.int16(): "int16",
        pa.int32(): "int32",
        pa.int64(): "int64",
        pa.uint8(): "uint8",
        pa.uint16(): "uint16",
        pa.uint32(): "uint32",
        pa.uint64(): "uint64",
        pa.float16(): "float16",
        pa.float32(): "float32",
        pa.float64(): "float64",
        pa.string(): "string",
        pa.bool_(): "bool",
        pa.date32(): "date",
        pa.date64(): "date",
    }
    for pa_type, name in type_map.items():
        if arrow_type == pa_type:
            return name

    type_str = str(arrow_type)
    if "timestamp" in type_str:
        return "datetime"
    return type_str
