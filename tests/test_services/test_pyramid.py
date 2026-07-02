"""Tests for the multi-resolution pyramid."""

import numpy as np
import pandas as pd
import pytest

from webchart.services.data import pyramid
from webchart.services.data.pyramid import (
    build_pyramid,
    level_for_zoom,
    read_pyramid_window,
)
from webchart.services.ingest.upload import convert_to_parquet


@pytest.fixture
def big_dataset(tmp_data_dir):
    """Ingest a 100k-row dataset with known global extrema and return its id."""
    n = 100_000
    rng = np.random.default_rng(0)
    x = np.arange(n)
    y = np.sin(x / 2000.0) * 50 + rng.normal(0, 3, n)
    y[40_000] = 9999.0   # global max
    y[70_000] = -9999.0  # global min
    df = pd.DataFrame({"x": x, "y": y, "z": np.sqrt(x)})
    src = tmp_data_dir / "uploads" / "big.csv"
    df.to_csv(src, index=False)
    convert_to_parquet(src, "big")
    return "big"


def test_build_creates_levels(big_dataset):
    info = build_pyramid(big_dataset, "x", columns=["y", "z"])
    assert info.max_level >= 1
    for level in range(info.max_level + 1):
        df = read_pyramid_window(big_dataset, "x", level)
        assert df is not None
        assert len(df) <= 2 * (2 ** level) + 2


def test_envelope_preserved_at_every_level(big_dataset):
    info = build_pyramid(big_dataset, "x", columns=["y", "z"])
    for level in range(info.max_level + 1):
        df = read_pyramid_window(big_dataset, "x", level, columns=["y", "z"])
        assert df["y"].max() == pytest.approx(9999.0)
        assert df["y"].min() == pytest.approx(-9999.0)


def test_meta_records_pyramid(big_dataset):
    build_pyramid(big_dataset, "x", columns=["y"])
    from webchart.services.data.access import get_dataset_meta

    meta = get_dataset_meta(big_dataset)
    assert "x" in meta.pyramids
    assert meta.pyramids["x"].method == "minmax"


def test_non_numeric_x_rejected(tmp_data_dir):
    df = pd.DataFrame({"cat": ["a", "b", "c"] * 10, "y": range(30)})
    src = tmp_data_dir / "uploads" / "cat.csv"
    df.to_csv(src, index=False)
    convert_to_parquet(src, "catds")
    from webchart.core.errors import DatasetProcessingError

    with pytest.raises(DatasetProcessingError):
        build_pyramid("catds", "cat", columns=["y"])


def test_level_for_zoom_falls_back_when_too_fine(big_dataset):
    info = build_pyramid(big_dataset, "x", columns=["y"])
    # A zoom far beyond max_level returns None -> caller uses base data
    assert level_for_zoom(info, info.max_level + 50) is None
    # Zoom 0 maps to a built level
    assert level_for_zoom(info, 0) is not None


def test_x_range_filter(big_dataset):
    info = build_pyramid(big_dataset, "x", columns=["y"])
    level = info.max_level
    full = read_pyramid_window(big_dataset, "x", level)
    windowed = read_pyramid_window(big_dataset, "x", level, x_range=(0, 1000))
    assert len(windowed) < len(full)
    # One neighbor point beyond the edge is kept so lines reach the tile edge.
    assert windowed["x"].max() <= 1000 + _neighbor_slack(full)


def _neighbor_slack(full_df):
    # spacing between adjacent pyramid points near x=1000, used as tolerance
    xs = full_df["x"].to_numpy()
    diffs = xs[1:] - xs[:-1]
    return float(diffs.max()) if len(diffs) else 0.0


def test_empty_window_returns_none_for_fallback(big_dataset):
    info = build_pyramid(big_dataset, "x", columns=["y"])
    # A range entirely outside the data yields no rows -> None (so the
    # renderer falls back to base instead of drawing a blank tile).
    out = read_pyramid_window(big_dataset, "x", info.max_level, x_range=(10**9, 10**9 + 1))
    assert out is None
