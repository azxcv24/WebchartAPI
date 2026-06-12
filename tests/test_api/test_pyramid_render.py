"""Integration: pyramid build endpoint + renderer actually using the pyramid."""

import time

import numpy as np
import pandas as pd
import pytest

from webchart.models.chart import ChartSpec
from webchart.models.dataset import DatasetMeta, DatasetStatus
from webchart.services.ingest.upload import convert_to_parquet


def _seed_dataset(dataset_id: str, n: int = 60_000) -> None:
    from webchart.config import settings

    x = np.arange(n)
    y = np.sin(x / 1500.0) * 20
    df = pd.DataFrame({"x": x, "y": y})
    src = settings.data_dir / "uploads" / f"{dataset_id}.csv"
    df.to_csv(src, index=False)
    convert_to_parquet(src, dataset_id)


def test_build_pyramid_endpoint_lifecycle(client, tmp_data_dir):
    _seed_dataset("pyr1")

    resp = client.post("/api/v1/datasets/pyr1/pyramid", json={"x_column": "x", "columns": ["y"]})
    assert resp.status_code == 202
    assert resp.json()["status"] == "building"

    # background build is synchronous under TestClient; poll the listing
    deadline = time.time() + 10
    while time.time() < deadline:
        listed = client.get("/api/v1/datasets/pyr1/pyramids").json()
        if "x" in listed:
            assert listed["x"]["method"] == "minmax"
            assert listed["x"]["max_level"] >= 1
            return
        time.sleep(0.05)
    raise AssertionError("pyramid never appeared in listing")


def test_build_pyramid_unknown_dataset_404(client, tmp_data_dir):
    resp = client.post("/api/v1/datasets/nope/pyramid", json={"x_column": "x"})
    assert resp.status_code == 404


async def test_renderer_uses_pyramid_when_present(tmp_data_dir, monkeypatch):
    """At low zoom with a pyramid, render_tile reads the pyramid, not base."""
    from webchart.services.data.pyramid import build_pyramid
    from webchart.services.render import tile_renderer

    _seed_dataset("pyr2")
    build_pyramid("pyr2", "x", columns=["y"])

    pyramid_calls = []
    base_calls = []

    real_pyramid = tile_renderer.read_pyramid_window
    real_base = tile_renderer.read_window

    def spy_pyramid(*args, **kwargs):
        pyramid_calls.append(args)
        return real_pyramid(*args, **kwargs)

    def spy_base(*args, **kwargs):
        base_calls.append(args)
        return real_base(*args, **kwargs)

    monkeypatch.setattr(tile_renderer, "read_pyramid_window", spy_pyramid)
    monkeypatch.setattr(tile_renderer, "read_window", spy_base)

    spec = ChartSpec(
        dataset_id="pyr2",
        x_axis={"column": "x"},
        y_axis={"column": "y"},
    )
    img = await tile_renderer.render_tile("pyr2", spec, zoom=0, tile_x=0)
    assert img[:8] == b"\x89PNG\r\n\x1a\n"
    assert pyramid_calls, "pyramid was not consulted"
    assert not base_calls, "base read should not happen when pyramid serves the tile"


async def test_renderer_falls_back_to_base_without_pyramid(tmp_data_dir, monkeypatch):
    from webchart.services.render import tile_renderer

    _seed_dataset("pyr3")  # no pyramid built

    base_calls = []
    real_base = tile_renderer.read_window

    def spy_base(*args, **kwargs):
        base_calls.append(args)
        return real_base(*args, **kwargs)

    monkeypatch.setattr(tile_renderer, "read_window", spy_base)

    spec = ChartSpec(
        dataset_id="pyr3",
        x_axis={"column": "x"},
        y_axis={"column": "y"},
    )
    img = await tile_renderer.render_tile("pyr3", spec, zoom=0, tile_x=0)
    assert img[:8] == b"\x89PNG\r\n\x1a\n"
    assert base_calls, "base read should be used when no pyramid exists"
