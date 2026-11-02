"""원본 저장소: x 범위 읽기를 O(범위)로 (계획 문서 2.7, D6 해소).

원본은 인제스트에서 정렬 컬럼(`DatasetMeta.sort_column`) 오름차순으로 작은 로우그룹
(기본 131,072행)에 나뉘어 저장된다. 각 로우그룹의 x 최솟값·최댓값(zone map)을 메모리에
두고, 범위 질의는 겹치는 로우그룹만 pyarrow로 읽는다. 전체 스캔은 범위가 전체일 때뿐이다.

정렬되지 않은 컬럼으로 질의해도 결과는 같다. zone map이 넓어 읽는 양이 늘 뿐이다.

좌표 규칙: x는 정규 좌표(services/data/x_domain.py)다. 범위는 반열린 구간 `[start, end)`.
"""

import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

from webchart.core.errors import DatasetNotFoundError, QueryTooLargeError
from webchart.domain.bucket_stats import FloatArray, IntArray
from webchart.domain.geometry import XRange
from webchart.services.data.x_domain import canonical_from_arrow

logger = logging.getLogger("webchart.infrastructure.raw_store")


def data_files(data_dir: Path) -> list[Path]:
    """데이터 디렉터리의 Parquet 파일을 저장 순서대로 (part.2가 part.10보다 앞)."""
    if not data_dir.exists():
        raise DatasetNotFoundError(data_dir.parent.name)
    return sorted(data_dir.glob("*.parquet"), key=_natural_key)


def _natural_key(path: Path) -> list[int | str]:
    return [int(token) if token.isdigit() else token for token in re.split(r"(\d+)", path.name)]


@dataclass(frozen=True, slots=True)
class ZoneMap:
    """한 컬럼의 로우그룹별 정규 좌표 최솟값·최댓값.

    배열의 i번째 원소가 저장 순서상 i번째 로우그룹이다. 통계가 없는 로우그룹은
    최솟값 -inf, 최댓값 +inf로 두어 어떤 범위와도 겹치게 한다 (안전한 쪽).
    """

    file_index: IntArray
    group_index: IntArray
    row_counts: IntArray
    x_min: FloatArray
    x_max: FloatArray

    def __len__(self) -> int:
        return len(self.file_index)

    def overlapping(self, x_range: XRange) -> IntArray:
        """범위와 겹치는 로우그룹 번호 (저장 순서)."""
        return np.flatnonzero((self.x_min < x_range.end) & (self.x_max >= x_range.start))

    def nearest_before(self, start: float) -> int | None:
        """start보다 작은 x 중 가장 큰 값을 가진 로우그룹 (최댓값 < start인 것 중에서)."""
        candidates = np.flatnonzero(self.x_max < start)
        if len(candidates) == 0:
            return None
        return int(candidates[np.argmax(self.x_max[candidates])])

    def nearest_after(self, end: float) -> int | None:
        """end 이상인 x 중 가장 작은 값을 가진 로우그룹 (최솟값 ≥ end인 것 중에서)."""
        candidates = np.flatnonzero(self.x_min >= end)
        if len(candidates) == 0:
            return None
        return int(candidates[np.argmin(self.x_min[candidates])])


@dataclass(frozen=True, slots=True)
class RangeRows:
    """범위 읽기 결과. x 오름차순이며 좌우 이웃 행을 포함할 수 있다.

    Attributes:
        x: 정규 좌표 (결측 없음)
        table: 요청한 컬럼들 (x와 같은 행 순서)
        lead_count / trail_count: 범위 앞·뒤 이웃 행 수 (0 또는 1)
    """

    x: FloatArray
    table: pa.Table
    lead_count: int
    trail_count: int

    @property
    def in_range(self) -> slice:
        return slice(self.lead_count, len(self.x) - self.trail_count)


class RawStore:
    """데이터셋 하나의 원본 Parquet 범위 읽기. 스레드 안전 (zone map 캐시에 잠금)."""

    def __init__(self, data_dir: Path) -> None:
        self._files = data_files(data_dir)
        self._zone_maps: dict[str, ZoneMap] = {}
        self._lock = threading.Lock()

    @property
    def files(self) -> list[Path]:
        return list(self._files)

    def zone_map(self, column: str) -> ZoneMap:
        with self._lock:
            cached = self._zone_maps.get(column)
        if cached is not None:
            return cached
        zone_map = _build_zone_map(self._files, column)
        with self._lock:
            self._zone_maps[column] = zone_map
        return zone_map

    def estimate_rows(self, x_column: str, x_range: XRange | None) -> int:
        """범위를 읽을 때 디스크에서 읽게 될 행 수 (겹치는 로우그룹 행 수의 합, 상한)."""
        zone_map = self.zone_map(x_column)
        if x_range is None:
            return int(zone_map.row_counts.sum())
        return int(zone_map.row_counts[zone_map.overlapping(x_range)].sum())

    def read_range(
        self,
        x_column: str,
        x_range: XRange | None,
        columns: list[str],
        include_neighbors: bool = False,
        max_rows: int | None = None,
    ) -> RangeRows:
        """x가 범위 안인 행을 x 오름차순으로 읽는다. x가 결측인 행은 버린다.

        include_neighbors면 범위 바로 앞(x < start 중 최대)과 바로 뒤(x ≥ end 중 최소)
        행을 하나씩 더한다. 선이 그림의 양 끝까지 이어지게 하는 데 쓴다.
        max_rows를 넘으면 QueryTooLargeError를 던진다 (이웃 행은 세지 않는다).
        """
        zone_map = self.zone_map(x_column)
        if x_range is None:
            groups = np.arange(len(zone_map))
        else:
            groups = zone_map.overlapping(x_range)
            if include_neighbors:
                extra = [
                    zone_map.nearest_before(x_range.start),
                    zone_map.nearest_after(x_range.end),
                ]
                groups = np.union1d(groups, [g for g in extra if g is not None]).astype(np.int64)

        if max_rows is not None:
            self._check_row_budget(x_column, x_range, groups, zone_map, max_rows)

        wanted = list(dict.fromkeys([x_column, *columns]))
        table = self._read_groups(zone_map, groups, wanted)
        x = canonical_from_arrow(table.column(x_column))
        return _select_range(x, table, x_range, include_neighbors)

    def iter_row_groups(self, columns: list[str]) -> Iterator[pa.Table]:
        """모든 로우그룹을 저장 순서대로 하나씩 읽는다 (피라미드 빌드용, 메모리 = 로우그룹 1개)."""
        for path in self._files:
            parquet = pq.ParquetFile(path, memory_map=True)
            for group in range(parquet.metadata.num_row_groups):
                yield parquet.read_row_group(group, columns=columns)

    def _check_row_budget(
        self,
        x_column: str,
        x_range: XRange | None,
        groups: IntArray,
        zone_map: ZoneMap,
        max_rows: int,
    ) -> None:
        upper_bound = int(zone_map.row_counts[groups].sum())
        if upper_bound <= max_rows:
            return
        # 상한이 넘으면 x 컬럼만 읽어 정확히 센다 (컬럼 하나라 싸다)
        x = canonical_from_arrow(self._read_groups(zone_map, groups, [x_column]).column(0))
        exact = int(np.count_nonzero(_range_mask(x, x_range)))
        if exact > max_rows:
            raise QueryTooLargeError(exact, max_rows)

    def _read_groups(self, zone_map: ZoneMap, groups: IntArray, columns: list[str]) -> pa.Table:
        pieces: list[pa.Table] = []
        for file_index in np.unique(zone_map.file_index[groups]):
            in_file = groups[zone_map.file_index[groups] == file_index]
            parquet = pq.ParquetFile(self._files[file_index], memory_map=True)
            pieces.append(
                parquet.read_row_groups(
                    [int(g) for g in zone_map.group_index[in_file]], columns=columns
                )
            )
        if not pieces:
            schema = pq.read_schema(self._files[0]) if self._files else pa.schema([])
            return schema.empty_table().select(columns)
        return pa.concat_tables(pieces)


def _range_mask(x: FloatArray, x_range: XRange | None) -> npt.NDArray[np.bool_]:
    valid = ~np.isnan(x)
    if x_range is None:
        return valid
    return valid & (x >= x_range.start) & (x < x_range.end)


def _select_range(
    x: FloatArray, table: pa.Table, x_range: XRange | None, include_neighbors: bool
) -> RangeRows:
    """읽은 로우그룹에서 범위 안 행과 이웃 행을 골라 x 오름차순으로 정리한다."""
    inside = _range_mask(x, x_range)
    picked = np.flatnonzero(inside)
    lead: list[int] = []
    trail: list[int] = []
    if include_neighbors and x_range is not None:
        before = np.flatnonzero(x < x_range.start)
        after = np.flatnonzero(x >= x_range.end)
        if len(before):
            # 같은 x가 여럿이면 저장 순서상 마지막 행 (정렬된 원본에서 바로 앞 행)
            lead = [int(before[len(before) - 1 - np.argmax(x[before][::-1])])]
        if len(after):
            trail = [int(after[np.argmin(x[after])])]

    rows = np.concatenate([lead, picked, trail]).astype(np.int64)
    selected_x = x[rows]
    if len(selected_x) > 1 and np.any(np.diff(selected_x) < 0):
        # 정렬되지 않은 원본: 안정 정렬로 같은 x끼리 저장 순서를 유지한다
        order = np.argsort(selected_x, kind="stable")
        rows, selected_x = rows[order], selected_x[order]
    return RangeRows(
        x=selected_x,
        table=table.take(pa.array(rows)),
        lead_count=len(lead),
        trail_count=len(trail),
    )


def _build_zone_map(files: list[Path], column: str) -> ZoneMap:
    """파일 footer의 로우그룹 통계만 읽어 zone map을 만든다 (데이터 본문은 읽지 않는다)."""
    file_index: list[int] = []
    group_index: list[int] = []
    row_counts: list[int] = []
    x_min: list[float] = []
    x_max: list[float] = []
    for index, path in enumerate(files):
        metadata = pq.ParquetFile(path).metadata
        column_position = _column_position(metadata, column)
        arrow_type = pq.read_schema(path).field(column).type
        for group in range(metadata.num_row_groups):
            row_group = metadata.row_group(group)
            low, high = _canonical_bounds(row_group, column_position, arrow_type)
            file_index.append(index)
            group_index.append(group)
            row_counts.append(row_group.num_rows)
            x_min.append(low)
            x_max.append(high)
    return ZoneMap(
        file_index=np.array(file_index, dtype=np.int64),
        group_index=np.array(group_index, dtype=np.int64),
        row_counts=np.array(row_counts, dtype=np.int64),
        x_min=np.array(x_min, dtype=np.float64),
        x_max=np.array(x_max, dtype=np.float64),
    )


def _column_position(metadata: pq.FileMetaData, column: str) -> int:
    for position in range(metadata.num_columns):
        if metadata.schema.column(position).path == column:
            return position
    raise KeyError(f"컬럼 '{column}'이 Parquet 스키마에 없다")


def _canonical_bounds(
    row_group: pq.RowGroupMetaData, column_position: int, arrow_type: pa.DataType
) -> tuple[float, float]:
    """로우그룹 통계 → 정규 좌표 (최솟값, 최댓값). 통계가 없으면 (-inf, +inf)."""
    statistics = row_group.column(column_position).statistics
    if statistics is None or not statistics.has_min_max:
        if statistics is not None and statistics.null_count == row_group.num_rows:
            return np.inf, -np.inf  # 전부 결측: 어떤 범위와도 겹치지 않는다
        return -np.inf, np.inf
    bounds = pa.array([statistics.min, statistics.max]).cast(arrow_type, safe=False)
    low, high = canonical_from_arrow(bounds)
    return float(low), float(high)


# --- 데이터셋별 인스턴스 캐시 ------------------------------------------------------

# zone map을 만들려면 모든 파일 footer를 읽어야 하므로 인스턴스를 재사용한다.
# 키에 data_version이 들어가 재인제스트하면 자연히 새 인스턴스를 쓴다. 이전 형식 데이터셋은
# data_version이 비어 있으므로 경로도 키에 넣는다.
_MAX_CACHED_STORES = 64
_stores: OrderedDict[tuple[str, str, str], RawStore] = OrderedDict()
_stores_lock = threading.Lock()


def raw_store_for(data_dir: Path, dataset_id: str, data_version: str) -> RawStore:
    key = (dataset_id, data_version, str(data_dir))
    with _stores_lock:
        store = _stores.get(key)
        if store is not None:
            _stores.move_to_end(key)
            return store
    store = RawStore(data_dir)
    with _stores_lock:
        _stores[key] = store
        while len(_stores) > _MAX_CACHED_STORES:
            _stores.popitem(last=False)
    return store


def forget_dataset(dataset_id: str) -> None:
    """데이터셋 삭제·재인제스트 시 캐시된 인스턴스를 버린다."""
    with _stores_lock:
        for key in [key for key in _stores if key[0] == dataset_id]:
            del _stores[key]
