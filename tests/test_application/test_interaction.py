"""인터랙션 질의 테스트: 크로스헤어(원본 값), 구간 통계(피라미드 + 원본 자투리)."""

import numpy as np
import pandas as pd
import pytest

from webchart.application.interaction import nearest_points, range_summary
from webchart.application.pyramids import build_pyramid
from webchart.config import settings
from webchart.domain.geometry import XRange
from webchart.services.data.access import get_dataset_meta
from webchart.services.ingest.upload import convert_to_parquet


def _ingest(frame: pd.DataFrame, dataset_id: str):
    source = settings.data_dir / "uploads" / f"{dataset_id}.parquet"
    frame.to_parquet(source, index=False)
    convert_to_parquet(source, dataset_id)
    return get_dataset_meta(dataset_id)


@pytest.fixture
def noisy(tmp_data_dir) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    n = 100_000
    x = np.sort(rng.uniform(0, 1_000, n))
    a = rng.normal(0, 1, n)
    b = rng.normal(5, 2, n)
    b[rng.random(n) < 0.1] = np.nan
    return pd.DataFrame({"x": x, "a": a, "b": b})


def _expected(frame: pd.DataFrame, column: str, start: float, end: float) -> dict:
    inside = frame[(frame.x >= start) & (frame.x < end)].dropna(subset=[column])
    values = inside[column]
    if values.empty:
        return {"count": 0}
    return {
        "count": len(values),
        "min": values.min(),
        "max": values.max(),
        "mean": values.mean(),
        "first": values.iloc[0],
        "last": values.iloc[-1],
    }


@pytest.mark.parametrize("with_pyramid", [False, True], ids=["raw", "pyramid+raw"])
@pytest.mark.parametrize("window", [(123.456, 789.012), (0.0, 1_000.0), (500.0, 500.5)])
def test_range_summary_is_exact(noisy, with_pyramid, window):
    meta = _ingest(noisy, "noisy")
    if with_pyramid:
        build_pyramid("noisy", "x", columns=["a", "b"])
        meta = get_dataset_meta("noisy")

    summary = range_summary(meta, "x", ["a", "b"], XRange(*window))

    assert summary.is_exact
    if with_pyramid and window[1] - window[0] > 1:
        assert summary.source.startswith("pyramid:")
    for column in ("a", "b"):
        expected = _expected(noisy, column, *window)
        actual = summary.series[column]
        assert actual.count == expected["count"]
        if expected["count"]:
            assert (actual.min, actual.max) == (expected["min"], expected["max"])
            assert (actual.first, actual.last) == (expected["first"], expected["last"])
            assert actual.mean == pytest.approx(expected["mean"], rel=1e-12)


def test_crosshair_skips_missing_values_per_series(tmp_data_dir):
    frame = pd.DataFrame({"x": [0.0, 1.0, 2.0, 3.0], "a": [10.0, 11.0, 12.0, 13.0],
                          "b": [20.0, np.nan, np.nan, 23.0]})  # fmt: skip
    meta = _ingest(frame, "gappy")

    points = {p.series: p for p in nearest_points(meta, "x", ["a", "b"], 1.2, 0.5)}

    assert (points["a"].x, points["a"].y) == (1.0, 11.0)
    assert (points["b"].x, points["b"].y) == (0.0, 20.0)  # b의 1.0·2.0은 결측


def test_crosshair_labels_time_in_display_timezone(tmp_data_dir):
    times = pd.date_range("2026-01-01", periods=10, freq="h", tz="UTC")
    meta = _ingest(pd.DataFrame({"t": times, "v": np.arange(10.0)}), "times")

    point = nearest_points(meta, "t", ["v"], float(times[3].value), 1e9, "Asia/Seoul")[0]
    assert point.label == "2026-01-01T12:00:00+09:00"
