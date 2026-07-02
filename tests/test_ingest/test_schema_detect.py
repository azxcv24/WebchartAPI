"""Tests for Parquet schema/statistics detection."""

import numpy as np
import pandas as pd
import pytest

from webchart.services.ingest.upload import convert_to_parquet


def test_stats_populated_after_ingest(tmp_data_dir):
    df = pd.DataFrame({"x": np.arange(5000), "y": np.linspace(-3.0, 7.0, 5000)})
    src = tmp_data_dir / "uploads" / "s.csv"
    df.to_csv(src, index=False)

    meta = convert_to_parquet(src, "ds")
    by_name = {c.name: c for c in meta.columns}

    # min/max must be read from Parquet row-group statistics. Regression:
    # _find_column_index used a non-existent schema.length attribute, so the
    # exception was swallowed and every column's stats were left None (which
    # also broke tile x-range computation, falling back to (0, 1)).
    assert by_name["x"].max_value == 5000 - 1
    assert by_name["x"].min_value == 0
    assert by_name["y"].max_value == pytest.approx(7.0)
    assert by_name["y"].min_value == pytest.approx(-3.0)
    assert by_name["x"].null_count == 0
