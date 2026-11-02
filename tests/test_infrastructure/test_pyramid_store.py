"""피라미드 v2 테스트: 레벨 구조, 집계 정확성, 낡은 피라미드 거부."""

import numpy as np
import pandas as pd
import pytest

from webchart.application.pyramids import build_pyramid
from webchart.application.windows import fetch_view_windows, usable_pyramid
from webchart.config import settings
from webchart.core.errors import DatasetProcessingError
from webchart.domain.bucket_stats import BucketStats, aggregate_sorted, merge_into
from webchart.domain.geometry import BucketGrid, XRange
from webchart.infrastructure.pyramid_store import choose_max_level
from webchart.services.data.access import get_dataset_meta
from webchart.services.ingest.upload import convert_to_parquet


def _ingest(frame: pd.DataFrame, dataset_id: str) -> None:
    source = settings.data_dir / "uploads" / f"{dataset_id}.parquet"
    frame.to_parquet(source, index=False)
    convert_to_parquet(source, dataset_id)


@pytest.fixture
def spiky(tmp_data_dir) -> pd.DataFrame:
    n = 100_000
    rng = np.random.default_rng(0)
    x = np.arange(n, dtype=np.float64)
    a = np.sin(x / 2000.0) * 50 + rng.normal(0, 3, n)
    b = np.cos(x / 1000.0)
    a[40_000], b[70_000] = 9_999.0, -8_000.0  # 시리즈마다 다른 위치의 스파이크
    b[10_000:10_050] = np.nan
    frame = pd.DataFrame({"x": x, "a": a, "b": b})
    _ingest(frame, "spiky")
    return frame


def test_levels_hold_every_series_extremes(spiky):
    info = build_pyramid("spiky", "x", columns=["a", "b"])
    reader = usable_pyramid(get_dataset_meta("spiky"), "x", ["a", "b"])
    assert reader is not None

    for level in range(info.max_level + 1):
        layer = reader.level(level)
        assert len(layer.row_count) == 2**level
        assert layer.row_count.sum() == len(spiky)
        whole = slice(0, 2**level)
        assert layer.stats("a", whole).max_y.max() == 9_999.0
        assert layer.stats("b", whole).min_y.min() == -8_000.0
        assert layer.stats("b", whole).has_gap.sum() >= 1  # 결측 구간이 기록된다


def test_finest_level_equals_direct_aggregation(spiky):
    """피라미드 버킷 = 원본을 같은 격자로 직접 집계한 결과 (모노이드 merge의 정확성)."""
    info = build_pyramid("spiky", "x", columns=["a"])
    reader = usable_pyramid(get_dataset_meta("spiky"), "x", ["a"])
    level = reader.level(info.max_level)

    grid = level.grid
    x = spiky["x"].to_numpy()
    index = np.clip(grid.index_of(x), 0, grid.bucket_count - 1)
    ids, part = aggregate_sorted(x, spiky["a"].to_numpy(), index)
    expected = BucketStats.empty(grid.bucket_count)
    merge_into(expected, ids, part)

    actual = level.stats("a", slice(0, grid.bucket_count))
    for name in ("count", "first_x", "last_y", "min_x", "min_y", "max_x", "max_y"):
        np.testing.assert_array_equal(getattr(actual, name), getattr(expected, name), name)


def test_time_axis_pyramid(tmp_data_dir):
    times = pd.date_range("2026-01-01", periods=20_000, freq="s", tz="UTC")
    _ingest(pd.DataFrame({"t": times, "v": np.arange(20_000.0)}), "timed")
    info = build_pyramid("timed", "t")
    assert info.columns == ["v"]
    assert info.domain_start == times[0].value


def test_reingest_makes_pyramid_stale(spiky):
    build_pyramid("spiky", "x", columns=["a"])
    assert usable_pyramid(get_dataset_meta("spiky"), "x", ["a"]) is not None

    _ingest(spiky, "spiky")  # 같은 ID로 다시 인제스트 → 새 data_version
    meta = get_dataset_meta("spiky")
    assert usable_pyramid(meta, "x", ["a"]) is None


def test_pyramid_without_requested_series_is_not_used(spiky):
    build_pyramid("spiky", "x", columns=["a"])
    assert usable_pyramid(get_dataset_meta("spiky"), "x", ["a", "b"]) is None


def test_approximate_view_still_reports_true_extremes(spiky, monkeypatch):
    """원본 예산을 넘겨 피라미드 근사로 집계해도 모든 극값이 남는다."""
    build_pyramid("spiky", "x", columns=["a", "b"])
    monkeypatch.setattr(settings, "exact_raw_row_budget", 10)
    meta = get_dataset_meta("spiky")
    grid = BucketGrid.covering(XRange(123.4, 99_000.7), 333)  # 레벨 경계와 맞지 않는 격자

    windows = fetch_view_windows(meta, "x", ["a", "b"], grid)

    assert not windows.is_exact and windows.source.startswith("pyramid:")
    assert windows.windows["a"].stats.max_y.max() == 9_999.0
    assert windows.windows["b"].stats.min_y.min() == -8_000.0


def test_rejects_non_axis_x_column(tmp_data_dir):
    _ingest(pd.DataFrame({"cat": ["a", "b", "c"] * 10, "y": range(30)}), "categorical")
    with pytest.raises(DatasetProcessingError):
        build_pyramid("categorical", "cat", columns=["y"])


@pytest.mark.parametrize(
    "rows, series, budget, expected",
    [
        (1_000_000, 1, 4 * 1024**3, 15),  # 행 수가 상한: 버킷당 16행 → 2^15 ≤ 62,500
        (10**10, 1, 4 * 1024**3, 24),  # 설정 상한 24
        (10**10, 10, 1024**2, 9),  # 메모리 예산이 상한
        (1, 1, 1024**3, 0),
    ],
)
def test_choose_max_level(rows, series, budget, expected):
    assert choose_max_level(rows, series, max_level=24, max_bytes=budget) == expected
