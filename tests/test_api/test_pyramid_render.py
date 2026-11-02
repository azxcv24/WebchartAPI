"""통합: 피라미드 빌드 API + 타일 렌더가 실제로 피라미드를 쓰는지."""

import time

import numpy as np
import pandas as pd

from webchart.application.tiles import PipelineTileRenderer, TileRequest, plan_frame
from webchart.application.windows import lattice_for
from webchart.config import settings
from webchart.domain.geometry import TileAddress
from webchart.models.chart import ChartSpec
from webchart.services.data.access import get_dataset_meta
from webchart.services.ingest.upload import convert_to_parquet


def _seed_dataset(dataset_id: str, n: int = 60_000) -> None:
    x = np.arange(n)
    df = pd.DataFrame({"x": x, "y": np.sin(x / 1500.0) * 20})
    src = settings.data_dir / "uploads" / f"{dataset_id}.csv"
    df.to_csv(src, index=False)
    convert_to_parquet(src, dataset_id)


def test_build_pyramid_endpoint_lifecycle(client, tmp_data_dir):
    _seed_dataset("pyr1")

    resp = client.post("/api/v1/datasets/pyr1/pyramid", json={"x_column": "x", "columns": ["y"]})
    assert resp.status_code == 202
    assert resp.json()["status"] == "building"

    deadline = time.time() + 10
    while time.time() < deadline:
        listed = client.get("/api/v1/datasets/pyr1/pyramids").json()
        if "x" in listed:
            assert listed["x"]["version"] == 3
            assert listed["x"]["method"] == "bucket_stats"
            assert listed["x"]["max_level"] >= 1
            return
        time.sleep(0.05)
    raise AssertionError("pyramid never appeared in listing")


def test_build_pyramid_unknown_dataset_404(client, tmp_data_dir):
    resp = client.post("/api/v1/datasets/nope/pyramid", json={"x_column": "x"})
    assert resp.status_code == 404


async def _render_tile(dataset_id: str, zoom: int):
    meta = get_dataset_meta(dataset_id)
    spec = ChartSpec(dataset_id=dataset_id, x_axis={"column": "x"}, y_axis={"column": "y"})
    lattice = lattice_for(meta, "x", settings.tile_size)
    first_tile = lattice.tile_range(TileAddress(zoom, 0))
    plan = plan_frame(meta, spec, first_tile, zoom, prefetch_radius=0)
    request = TileRequest(meta, spec, plan.lattice, TileAddress(zoom, 0), 1, plan.y_ranges)
    return await PipelineTileRenderer().render(request)


async def test_tile_uses_pyramid_when_level_is_built(tmp_data_dir):
    from webchart.application.pyramids import build_pyramid

    _seed_dataset("pyr2")
    info = build_pyramid("pyr2", "x", columns=["y"])

    tile = await _render_tile("pyr2", zoom=0)
    assert tile.image[:8] == b"\x89PNG\r\n\x1a\n"
    assert (tile.source, tile.is_exact) == ("pyramid:L9", True)

    # 피라미드 최대 레벨보다 깊으면 원본을 읽는다 (여전히 정확)
    deep = await _render_tile("pyr2", zoom=info.max_level)
    assert (deep.source, deep.is_exact) == ("raw", True)


async def test_tile_reads_raw_without_pyramid(tmp_data_dir):
    _seed_dataset("pyr3")
    tile = await _render_tile("pyr3", zoom=0)
    assert tile.image[:8] == b"\x89PNG\r\n\x1a\n"
    assert (tile.source, tile.is_exact) == ("raw", True)
