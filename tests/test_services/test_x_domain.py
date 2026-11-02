"""x축 정규 좌표 변환 테스트."""

import datetime as dt

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from webchart.models.dataset import ColumnInfo, ColumnKind
from webchart.services.data.x_domain import (
    canonical_scalar,
    column_kind_of,
    format_x,
    to_canonical,
    to_native_bound,
)

NOON_UTC_NS = pd.Timestamp("2026-01-01T12:00:00Z").value


@pytest.mark.parametrize(
    "arrow_type, kind",
    [
        (pa.int64(), ColumnKind.NUMERIC),
        (pa.float32(), ColumnKind.NUMERIC),
        (pa.decimal128(10, 2), ColumnKind.NUMERIC),
        (pa.timestamp("ns"), ColumnKind.TIME),
        (pa.timestamp("us", tz="UTC"), ColumnKind.TIME),
        (pa.date32(), ColumnKind.TIME),
        (pa.string(), ColumnKind.OTHER),
        (pa.bool_(), ColumnKind.OTHER),
    ],
)
def test_column_kind_of(arrow_type, kind):
    assert column_kind_of(arrow_type) is kind


class TestToCanonical:
    def test_numeric_passes_through_with_nan(self):
        values = pd.Series([1, None, 3], dtype="Int64")
        np.testing.assert_array_equal(to_canonical(values), [1.0, np.nan, 3.0])

    def test_naive_time_is_wall_clock_epoch_ns(self):
        values = pd.Series(pd.to_datetime(["2026-01-01 12:00:00", None]))
        result = to_canonical(values)
        assert result[0] == NOON_UTC_NS
        assert np.isnan(result[1])  # NaT는 int64 최솟값이 아니라 NaN이어야 한다

    def test_aware_time_is_utc_epoch_ns(self):
        seoul_9pm = pd.Series([pd.Timestamp("2026-01-01T21:00:00+09:00")])
        assert to_canonical(seoul_9pm)[0] == NOON_UTC_NS

    def test_non_ns_unit_is_normalized(self):
        micros = pd.Series(pd.to_datetime(["2026-01-01 12:00:00"]).as_unit("us"))
        assert to_canonical(micros)[0] == NOON_UTC_NS

    def test_date_objects(self):
        dates = pd.Series([dt.date(2026, 1, 1)], dtype=object)
        assert to_canonical(dates)[0] == pd.Timestamp("2026-01-01").value

    @pytest.mark.parametrize("values", [pd.Series(["a", "b"]), pd.Series([True, False])])
    def test_rejects_non_axis_columns(self, values):
        with pytest.raises(TypeError):
            to_canonical(values)


def test_canonical_scalar_matches_to_canonical_for_time():
    stat = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    assert canonical_scalar(stat, ColumnKind.TIME) == NOON_UTC_NS
    assert canonical_scalar("x", ColumnKind.OTHER) is None


class TestNativeBoundAndFormat:
    naive = ColumnInfo(name="t", dtype="datetime", kind=ColumnKind.TIME)
    aware = ColumnInfo(name="t", dtype="datetime", kind=ColumnKind.TIME, timezone="Asia/Seoul")

    def test_numeric_bound_unchanged(self):
        column = ColumnInfo(name="x", dtype="float64", kind=ColumnKind.NUMERIC)
        assert to_native_bound(1.5, column) == 1.5

    def test_time_bounds_roundtrip(self):
        assert to_native_bound(NOON_UTC_NS, self.naive) == pd.Timestamp("2026-01-01 12:00")
        assert to_native_bound(NOON_UTC_NS, self.aware) == pd.Timestamp("2026-01-01T12:00Z")

    def test_format_shows_original_timezone(self):
        assert format_x(NOON_UTC_NS, self.naive) == "2026-01-01T12:00:00"
        assert format_x(NOON_UTC_NS, self.aware) == "2026-01-01T21:00:00+09:00"


def test_legacy_meta_without_kind_is_backfilled():
    legacy = ColumnInfo.model_validate(
        {"name": "x", "dtype": "int64", "min_value": 0, "max_value": 10}
    )
    assert legacy.kind is ColumnKind.NUMERIC
    assert (legacy.min_numeric, legacy.max_numeric) == (0.0, 10.0)
