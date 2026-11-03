"""Tests for downsampling algorithms."""

import pandas as pd
import pytest

from webchart.services.data.downsample import downsample


@pytest.fixture
def large_df():
    return pd.DataFrame(
        {
            "x": range(10000),
            "y": [i * 2.0 + (i % 17) for i in range(10000)],
            "z": [i**0.5 for i in range(10000)],
        }
    )


class TestLTTB:
    def test_reduces_to_target(self, large_df):
        result = downsample(large_df, "x", "y", 500, algorithm="lttb")
        assert len(result) == 500

    def test_keeps_first_and_last(self, large_df):
        result = downsample(large_df, "x", "y", 100, algorithm="lttb")
        assert result["x"].iloc[0] == 0
        assert result["x"].iloc[-1] == 9999

    def test_noop_when_under_target(self, large_df):
        small = large_df.head(50)
        result = downsample(small, "x", "y", 100, algorithm="lttb")
        assert len(result) == 50

    def test_preserves_columns(self, large_df):
        result = downsample(large_df, "x", "y", 500, algorithm="lttb")
        assert list(result.columns) == ["x", "y", "z"]


class TestMinMax:
    def test_reduces_to_target(self, large_df):
        result = downsample(large_df, "x", "y", 500, algorithm="minmax")
        assert len(result) <= 500

    def test_contains_extremes(self, large_df):
        result = downsample(large_df, "x", "y", 100, algorithm="minmax")
        # Min-max should capture the global min and max
        assert result["y"].min() <= large_df["y"].min() + 1
        assert result["y"].max() >= large_df["y"].max() - 1


class TestAverage:
    def test_reduces_to_target(self, large_df):
        result = downsample(large_df, "x", "y", 500, algorithm="average")
        assert len(result) == 500

    def test_averaged_values(self, large_df):
        result = downsample(large_df, "x", "y", 100, algorithm="average")
        # Average of first bucket should be close to average of first 100 points
        first_bucket_mean = large_df.head(100)["y"].mean()
        assert abs(result["y"].iloc[0] - first_bucket_mean) < 1.0
