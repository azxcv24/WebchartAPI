"""원본 저장소 테스트: 정렬 인제스트, zone map 범위 읽기, 이웃 행 (D6)."""

import numpy as np
import pandas as pd
import pytest

from webchart.config import settings
from webchart.core.errors import QueryTooLargeError
from webchart.domain.geometry import XRange
from webchart.infrastructure.raw_store import RawStore
from webchart.services.ingest.sorted_layout import is_sorted_on_disk
from webchart.services.ingest.upload import convert_to_parquet

ROW_GROUP_ROWS = 1_000


@pytest.fixture(autouse=True)
def small_row_groups(monkeypatch):
    """로우그룹을 작게 해 작은 데이터로도 여러 로우그룹을 만든다."""
    monkeypatch.setattr(settings, "ingest_row_group_rows", ROW_GROUP_ROWS)


def _ingest(frame: pd.DataFrame, dataset_id: str, suffix: str = "parquet", **kwargs):
    source = settings.data_dir / "uploads" / f"{dataset_id}.{suffix}"
    if suffix == "csv":
        frame.to_csv(source, index=False)
    else:
        frame.to_parquet(source, index=False)
    meta = convert_to_parquet(source, dataset_id, **kwargs)
    return meta, RawStore(settings.data_dir / "datasets" / dataset_id / "data")


def test_unsorted_source_is_stored_sorted():
    rng = np.random.default_rng(0)
    frame = pd.DataFrame({"x": rng.permutation(10_000).astype(float), "y": rng.normal(size=10_000)})
    meta, store = _ingest(frame, "shuffled")

    assert meta.sort_column == "x"
    assert meta.data_version
    assert is_sorted_on_disk(settings.data_dir / "datasets" / "shuffled" / "data", "x")
    # 정렬되면 로우그룹 x 범위가 겹치지 않는다
    zone_map = store.zone_map("x")
    assert len(zone_map) >= 10
    assert np.all(zone_map.x_min[1:] > zone_map.x_max[:-1])


def test_range_read_touches_only_overlapping_row_groups():
    frame = pd.DataFrame({"x": np.arange(20_000.0), "y": np.arange(20_000.0) * 2})
    _, store = _ingest(frame, "ranged")

    window = XRange(5_000.0, 5_500.0)
    assert store.estimate_rows("x", window) <= 2 * ROW_GROUP_ROWS
    rows = store.read_range("x", window, ["y"])

    assert rows.x[0] == 5_000.0 and rows.x[-1] == 5_499.0  # 반열린 구간
    np.testing.assert_array_equal(rows.table.column("y").to_numpy(), rows.x * 2)


def test_neighbors_come_from_adjacent_row_groups():
    frame = pd.DataFrame({"x": np.arange(0.0, 20_000.0, 2.0), "y": np.ones(10_000)})
    _, store = _ingest(frame, "gapped")

    # 범위가 로우그룹 경계(2000)에서 시작해 앞 이웃은 이전 로우그룹에만 있다
    rows = store.read_range("x", XRange(2_000.0, 2_001.0), ["y"], include_neighbors=True)

    assert (rows.lead_count, rows.trail_count) == (1, 1)
    assert list(rows.x) == [1_998.0, 2_000.0, 2_002.0]
    assert list(rows.x[rows.in_range]) == [2_000.0]


def test_explicit_sort_column_keeps_other_columns_readable():
    rng = np.random.default_rng(1)
    frame = pd.DataFrame({"t": np.arange(5_000.0), "v": rng.permutation(5_000).astype(float)})
    meta, store = _ingest(frame, "by-v", sort_column="v")
    assert meta.sort_column == "v"

    # 정렬 컬럼이 아닌 t로 질의해도 결과는 정확하다 (읽는 양만 많다)
    rows = store.read_range("t", XRange(100.0, 110.0), ["v"], include_neighbors=True)
    assert list(rows.x) == [99.0, *np.arange(100.0, 110.0), 110.0]


def test_time_axis_range_in_canonical_nanoseconds():
    times = pd.date_range("2026-01-01", periods=5_000, freq="s", tz="Asia/Seoul")
    frame = pd.DataFrame({"ts": times.astype(str), "y": np.arange(5_000.0)})
    meta, store = _ingest(frame, "seoul", suffix="csv")
    assert meta.sort_column == "ts"

    start = times[1_000].value  # tz-aware → UTC 나노초
    rows = store.read_range("ts", XRange(float(start), float(start + 10 * 10**9)), ["y"])
    assert list(rows.table.column("y").to_numpy()) == list(np.arange(1_000.0, 1_010.0))


def test_missing_x_rows_are_dropped():
    frame = pd.DataFrame({"x": [3.0, np.nan, 1.0, 2.0], "y": [30.0, 99.0, 10.0, 20.0]})
    _, store = _ingest(frame, "with-nan")
    rows = store.read_range("x", None, ["y"])
    assert list(rows.x) == [1.0, 2.0, 3.0]
    assert list(rows.table.column("y").to_numpy()) == [10.0, 20.0, 30.0]


def test_row_budget_counts_exact_rows_in_range():
    frame = pd.DataFrame({"x": np.arange(10_000.0), "y": np.zeros(10_000)})
    _, store = _ingest(frame, "budget")

    # 로우그룹 단위 상한(1,000행)은 넘지만 실제 범위 행 수(600)는 예산 안
    rows = store.read_range("x", XRange(200.0, 800.0), ["y"], max_rows=700)
    assert len(rows.x) == 600
    with pytest.raises(QueryTooLargeError):
        store.read_range("x", XRange(0.0, 5_000.0), ["y"], max_rows=700)


def test_date_only_column_becomes_time_axis():
    import datetime as dt

    frame = pd.DataFrame({"day": [dt.date(2026, 1, d) for d in range(1, 11)], "y": np.arange(10.0)})
    meta, store = _ingest(frame, "dates")
    day = next(c for c in meta.columns if c.name == "day")
    assert day.kind.value == "time"
    assert day.min_numeric == pd.Timestamp("2026-01-01").value
    rows = store.read_range("day", None, ["y"])
    assert len(rows.x) == 10
