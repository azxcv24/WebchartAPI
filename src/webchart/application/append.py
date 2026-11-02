"""실시간 추가 인제스트 (계획 문서 4.3).

추가 한 번의 순서
1. 행을 데이터셋 스키마로 맞추고 정렬 컬럼 순으로 새 Parquet 파일 하나에 쓴다.
2. 축 컬럼(정렬 컬럼, 피라미드 x 컬럼)마다 그림이 바뀌는 x 구간을 구한다. 추가된 행의
   x 범위를 그 앞뒤로 이어지는 기존 행까지 넓힌 구간이다 (선이 그 행들과 이어지므로).
3. 피라미드: 추가된 행이 격자 밖이면 격자를 2배씩 넓힌 새 저장소를 만들고(grow_storage),
   추가된 행을 버킷에 merge한다 (BucketStats 모노이드라 다시 집계하지 않아도 정확하다).
4. meta.json을 한 번에 바꾼다 (행 수, 통계, 세대, 추가 기록, 격자, 피라미드).
   이때부터 새 데이터가 보인다.
5. 그 데이터셋을 보는 WS 세션에 알린다.

타일 캐시: 타일 키에 "그 타일 구간을 바꾼 마지막 추가의 세대"(tile epoch)가 들어간다.
추가가 닿지 않은 타일은 키가 그대로라 캐시를 계속 쓴다. 단, 격자가 넓어지거나 GLOBAL
y범위가 바뀌면(새 최댓값 등) 모든 타일 키가 바뀐다 (그림 자체가 달라지므로 당연하다).
"""

import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from webchart.application.live import DataAppended, dataset_events
from webchart.application.windows import lattice_for, pyramid_dir, usable_pyramid
from webchart.config import settings
from webchart.core.errors import DatasetProcessingError
from webchart.domain.geometry import XRange
from webchart.infrastructure.pyramid_store import (
    grow_storage,
    merge_rows,
    new_storage_name,
    remove_old_storage,
)
from webchart.infrastructure.raw_store import data_files
from webchart.models.dataset import AppendRecord, ColumnKind, DatasetMeta
from webchart.services.data.access import get_dataset_meta, raw_store, save_dataset_meta
from webchart.services.data.x_domain import canonical_from_arrow, canonical_scalar

logger = logging.getLogger("webchart.application.append")

# 추가 기록 상한. 넘으면 오래된 절반을 한 기록으로 합친다 (구간은 합집합 → 보수적으로 무효화)
MAX_APPEND_LOG = 512

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True, slots=True)
class AppendResult:
    dataset_id: str
    generation: int
    rows: int
    ranges: dict[str, tuple[float, float]]
    grown_columns: list[str]  # 격자가 넓어진 x 컬럼 (그 컬럼의 타일은 모두 새로 그린다)


def append_columns(dataset_id: str, columns: dict[str, list[Any]]) -> AppendResult:
    """열 형식 데이터 `{컬럼: [값...]}`를 데이터셋에 추가한다. 데이터셋마다 한 번에 하나씩."""
    with _lock_for(dataset_id):
        meta = get_dataset_meta(dataset_id)
        table = _conform(meta, columns)
        if table.num_rows == 0:
            raise DatasetProcessingError("추가할 행이 없다")

        axis_columns = _axis_columns(meta)
        # 그림이 바뀌는 구간은 추가 전 원본에서 이웃 행을 찾아 구한다
        ranges: dict[str, tuple[float, float]] = {}
        for name in axis_columns:
            affected = _affected_range(meta, name, table)
            if affected is not None:
                ranges[name] = affected

        # 격자를 고정한다 (처음 추가하는 축 컬럼은 지금 통계로). 피라미드가 있는 컬럼은 그 격자
        for name in axis_columns:
            if name not in meta.x_domains:
                meta.x_domains[name] = lattice_for(meta, name, tile_size_px=1).domain.as_tuple()

        _write_rows(meta, table)
        meta.generation += 1
        meta.row_count += table.num_rows
        meta.partition_count += 1
        _merge_column_stats(meta, table)

        grown = _update_pyramids(meta, table)
        _grow_free_domains(meta, table, axis_columns, grown)
        meta.append_log.append(AppendRecord(generation=meta.generation, ranges=ranges))
        _compact_log(meta)
        save_dataset_meta(meta)
        for x_column in grown:
            if x_column in meta.pyramids:
                remove_old_storage(
                    pyramid_dir(dataset_id, x_column), keep=meta.pyramids[x_column].storage
                )

    dataset_events.publish(DataAppended(dataset_id, meta.generation, ranges))
    logger.info(f"추가: {dataset_id} +{table.num_rows}행 (세대 {meta.generation})")
    return AppendResult(dataset_id, meta.generation, table.num_rows, ranges, grown)


def tile_epoch(meta: DatasetMeta, x_column: str, x_range: XRange) -> int:
    """이 x 구간의 그림을 바꾼 마지막 추가의 세대 (없으면 0)."""
    epoch = 0
    for record in meta.append_log:
        bounds = record.ranges.get(x_column)
        if bounds is None or (bounds[0] < x_range.end and bounds[1] >= x_range.start):
            epoch = max(epoch, record.generation)
    return epoch


def _lock_for(dataset_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(dataset_id, threading.Lock())


def _axis_columns(meta: DatasetMeta) -> list[str]:
    names = [meta.sort_column] if meta.sort_column else []
    names += list(meta.pyramids) + list(meta.x_domains)
    return list(dict.fromkeys(names))


def _conform(meta: DatasetMeta, columns: dict[str, list[Any]]) -> pa.Table:
    """입력을 데이터셋 스키마(첫 데이터 파일)로 맞춘다. 없는 컬럼은 결측, 모르는 컬럼은 오류."""
    schema = pq.read_schema(data_files(_data_dir(meta))[0])
    unknown = set(columns) - set(schema.names)
    if unknown:
        raise DatasetProcessingError(f"데이터셋에 없는 컬럼: {sorted(unknown)}")
    lengths = {len(values) for values in columns.values()}
    if len(lengths) > 1:
        raise DatasetProcessingError(f"컬럼 길이가 서로 다르다: {sorted(lengths)}")
    rows = lengths.pop() if lengths else 0

    arrays = []
    for field in schema:
        values = columns.get(field.name)
        if values is None:
            arrays.append(pa.nulls(rows, field.type))
            continue
        if pa.types.is_timestamp(field.type):
            parsed = pd.to_datetime(
                pd.Series(values), utc=field.type.tz is not None, format="ISO8601"
            )
            arrays.append(pa.array(parsed, type=field.type))
        else:
            try:
                arrays.append(pa.array(values).cast(field.type))
            except (pa.ArrowInvalid, pa.ArrowTypeError) as error:
                raise DatasetProcessingError(
                    f"컬럼 '{field.name}' 값을 {field.type}로 바꿀 수 없다: {error}"
                ) from error
    table = pa.Table.from_arrays(arrays, schema=schema.remove_metadata())
    if meta.sort_column:
        table = table.sort_by(meta.sort_column)
    return table


def _write_rows(meta: DatasetMeta, table: pa.Table) -> None:
    data_dir = _data_dir(meta)
    numbers = [
        int(part)
        for path in data_files(data_dir)
        for part in path.stem.split(".")
        if part.isdigit()
    ]
    path = data_dir / f"part.{max(numbers, default=-1) + 1}.parquet"
    staging = path.with_suffix(".tmp")
    pq.write_table(table, staging, row_group_size=settings.ingest_row_group_rows)
    staging.rename(path)  # 읽는 쪽이 반쯤 쓴 파일을 보지 않게


def _affected_range(meta: DatasetMeta, column: str, table: pa.Table) -> tuple[float, float] | None:
    """추가된 행의 x 범위를 앞뒤 기존 행까지 넓힌 닫힌 구간."""
    x = canonical_from_arrow(table.column(column))
    x = x[~np.isnan(x)]
    if len(x) == 0:
        return None
    low, high = float(x.min()), float(x.max())
    rows = raw_store(meta).read_range(
        column, XRange(low, math.nextafter(high, math.inf)), [], include_neighbors=True
    )
    before = rows.x[: rows.lead_count]
    after = rows.x[len(rows.x) - rows.trail_count :]
    return (float(before[0]) if len(before) else low, float(after[0]) if len(after) else high)


def _merge_column_stats(meta: DatasetMeta, table: pa.Table) -> None:
    for column in meta.columns:
        values = table.column(column.name)
        column.null_count += values.null_count
        if column.kind is ColumnKind.OTHER or values.null_count == len(values):
            continue
        bounds = pc.min_max(values)
        low, high = bounds["min"].as_py(), bounds["max"].as_py()
        low_numeric = canonical_scalar(low, column.kind)
        high_numeric = canonical_scalar(high, column.kind)
        if low_numeric is None or high_numeric is None:
            continue
        if column.min_numeric is None or low_numeric < column.min_numeric:
            column.min_numeric = low_numeric
            column.min_value = (
                float(low) if column.kind is ColumnKind.NUMERIC else pd.Timestamp(low).isoformat()
            )
        if column.max_numeric is None or high_numeric > column.max_numeric:
            column.max_numeric = high_numeric
            column.max_value = (
                float(high) if column.kind is ColumnKind.NUMERIC else pd.Timestamp(high).isoformat()
            )


def _update_pyramids(meta: DatasetMeta, table: pa.Table) -> list[str]:
    """모든 피라미드에 추가된 행을 merge한다. 격자가 넓어진 x 컬럼 목록을 반환한다.

    격자를 넓힐 때는 새 storage 디렉터리를 만든다. meta.json이 바뀌기 전까지 읽는 쪽은
    이전 디렉터리를 계속 쓰므로, 이전 디렉터리는 meta를 저장한 뒤에 지운다 (append_columns).
    """
    grown = []
    for x_column, info in list(meta.pyramids.items()):
        if usable_pyramid(meta, x_column, info.columns) is None:
            continue  # 낡은 형식이나 다른 data_version의 피라미드는 건드리지 않는다
        assert info.domain_start is not None and info.domain_end is not None
        x = canonical_from_arrow(table.column(x_column))
        has_x = ~np.isnan(x)
        values = {s: canonical_from_arrow(table.column(s))[has_x] for s in info.columns}
        x = x[has_x]
        if len(x) == 0:
            continue
        directory = pyramid_dir(meta.dataset_id, x_column)
        start, end = info.domain_start, info.domain_end
        while x.min() < start or x.max() >= end:
            info = grow_storage(
                directory / info.storage, directory / new_storage_name(), info,
                toward_end=bool(x.max() >= end),
            )  # fmt: skip
            assert info.domain_start is not None and info.domain_end is not None
            start, end = info.domain_start, info.domain_end
            if x_column not in grown:
                grown.append(x_column)
        merge_rows(directory / info.storage, info, x, values)
        meta.pyramids[x_column] = info
        meta.x_domains[x_column] = (start, end)
    return grown


def _grow_free_domains(
    meta: DatasetMeta, table: pa.Table, axis_columns: list[str], grown: list[str]
) -> None:
    """피라미드가 없는 축 컬럼의 고정 격자도 추가된 행을 덮도록 2배씩 넓힌다."""
    for name in axis_columns:
        if name in meta.pyramids:
            continue
        x = canonical_from_arrow(table.column(name))
        x = x[~np.isnan(x)]
        if len(x) == 0:
            continue
        start, end = meta.x_domains[name]
        while x.min() < start or x.max() >= end:
            width = end - start
            start, end = (start, end + width) if x.max() >= end else (start - width, end)
            if name not in grown:
                grown.append(name)
        meta.x_domains[name] = (start, end)


def _compact_log(meta: DatasetMeta) -> None:
    if len(meta.append_log) <= MAX_APPEND_LOG:
        return
    half = len(meta.append_log) // 2
    old, recent = meta.append_log[:half], meta.append_log[half:]
    merged: dict[str, tuple[float, float]] = {}
    shared = set.intersection(*(set(record.ranges) for record in old))
    for name in shared:
        merged[name] = (min(r.ranges[name][0] for r in old), max(r.ranges[name][1] for r in old))
    meta.append_log = [AppendRecord(generation=old[-1].generation, ranges=merged), *recent]


def _data_dir(meta: DatasetMeta) -> Path:
    return settings.data_dir / "datasets" / meta.dataset_id / "data"
