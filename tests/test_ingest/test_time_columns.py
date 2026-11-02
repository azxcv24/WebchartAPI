"""시간 컬럼 감지·변환과 시간축 메타데이터 테스트 (D3 회귀 방지)."""

import pandas as pd
import pytest

from webchart.models.dataset import ColumnKind
from webchart.services.ingest.time_columns import TimeColumn, detect_time_columns
from webchart.services.ingest.upload import convert_to_parquet


class TestDetect:
    def test_detects_naive_and_offset_columns(self):
        sample = pd.DataFrame(
            {
                "naive": ["2026-01-01 00:00:00", "2026-01-01 00:00:01"],
                "offset": ["2026-01-01T00:00:00+09:00", "2026-01-01T00:00:00+00:00"],
                "value": [1.0, 2.0],
            }
        )
        assert detect_time_columns(sample) == [
            TimeColumn("naive", is_tz_aware=False),
            TimeColumn("offset", is_tz_aware=True),
        ]

    @pytest.mark.parametrize(
        "values",
        [
            ["cat_1", "cat_2"],
            ["2026-01-01", "not a date"],  # 하나라도 실패하면 범주형으로 본다
            [None, None],
        ],
    )
    def test_ignores_non_time_strings(self, values):
        assert detect_time_columns(pd.DataFrame({"c": values})) == []


def _ingest_csv(tmp_data_dir, frame: pd.DataFrame, dataset_id: str):
    source = tmp_data_dir / "uploads" / f"{dataset_id}.csv"
    frame.to_csv(source, index=False)
    return convert_to_parquet(source, dataset_id)


def test_csv_time_column_gets_canonical_bounds(tmp_data_dir):
    frame = pd.DataFrame(
        {
            "ts": pd.date_range("2026-01-01", periods=100, freq="s").astype(str),
            "v": range(100),
        }
    )
    meta = _ingest_csv(tmp_data_dir, frame, "csv-time")
    ts = next(c for c in meta.columns if c.name == "ts")

    assert ts.kind is ColumnKind.TIME
    assert ts.timezone is None
    assert ts.min_value == "2026-01-01T00:00:00"
    assert ts.min_numeric == pd.Timestamp("2026-01-01").value
    assert ts.max_numeric == pd.Timestamp("2026-01-01 00:01:39").value


def test_csv_offsets_are_stored_as_utc(tmp_data_dir):
    frame = pd.DataFrame({"ts": ["2026-01-01T09:00:00+09:00", "2026-01-01T01:00:00+00:00"]})
    meta = _ingest_csv(tmp_data_dir, frame, "csv-offset")
    ts = next(c for c in meta.columns if c.name == "ts")

    assert ts.kind is ColumnKind.TIME
    assert ts.timezone == "UTC"
    assert ts.min_numeric == pd.Timestamp("2026-01-01T00:00:00Z").value


def test_category_strings_stay_strings(tmp_data_dir, sample_csv):
    meta = convert_to_parquet(sample_csv, "categories")
    category = next(c for c in meta.columns if c.name == "category")
    assert category.kind is ColumnKind.OTHER
    assert category.min_numeric is None
