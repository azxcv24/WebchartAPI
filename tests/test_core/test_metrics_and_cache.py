"""L1 캐시와 지표 테스트 (Phase 4.1)."""

import numpy as np
import pandas as pd

from webchart.application.tiles import (
    CachedTileRenderer,
    PipelineTileRenderer,
    TileRequest,
    plan_frame,
)
from webchart.application.windows import lattice_for
from webchart.cache.memory_cache import ByteLRU
from webchart.config import settings
from webchart.core.metrics import Metrics, metrics, source_kind
from webchart.domain.geometry import TileAddress
from webchart.models.chart import ChartSpec
from webchart.services.data.access import get_dataset_meta
from webchart.services.ingest.upload import convert_to_parquet


def test_byte_lru_evicts_oldest_by_size():
    cache = ByteLRU(capacity_bytes=10)
    cache.put("a", b"1234")
    cache.put("b", b"1234")
    assert cache.get("a") == b"1234"  # a가 최근 사용이 된다
    cache.put("c", b"1234")  # 12바이트 > 10 → 가장 오래된 b를 버린다
    assert cache.get("b") is None and cache.get("a") and cache.get("c")
    assert cache.size_bytes == 8
    cache.put("huge", b"x" * 11)  # 상한보다 큰 항목은 넣지 않는다
    assert cache.get("huge") is None
    assert cache.discard_prefix("a") == 1 and cache.size_bytes == 4


async def test_l1_then_l2_then_render(tmp_data_dir, fake_redis):
    x = np.arange(5_000, dtype=float)
    source = tmp_data_dir / "uploads" / "m.parquet"
    pd.DataFrame({"x": x, "y": np.sin(x)}).to_parquet(source, index=False)
    convert_to_parquet(source, "m")
    meta = get_dataset_meta("m")
    spec = ChartSpec(dataset_id="m", x_axis={"column": "x"}, y_columns=["y"])
    lattice = lattice_for(meta, "x", settings.tile_size)
    plan = plan_frame(meta, spec, lattice.domain, 0, prefetch_radius=0)
    request = TileRequest(meta, spec, lattice, TileAddress(0, 0), 1, plan.y_ranges)

    memory = ByteLRU(10**7)
    renderer = CachedTileRenderer(PipelineTileRenderer(), memory)
    before = {o: metrics.counter("webchart_tile_requests_total", outcome=o)
              for o in ("rendered", "l1_hit", "l2_hit")}  # fmt: skip

    first = await renderer.render(request)
    second = await renderer.render(request)
    memory.clear()
    third = await renderer.render(request)

    assert first.source == "raw" and second.source == third.source == "cache"
    assert first.image == second.image == third.image
    for outcome in ("rendered", "l1_hit", "l2_hit"):
        assert (
            metrics.counter("webchart_tile_requests_total", outcome=outcome) == before[outcome] + 1
        )


def test_prometheus_text():
    registry = Metrics()
    registry.increment("requests_total", "요청 수", outcome="hit")
    registry.observe("latency_seconds", "지연", 0.03, source="raw")
    text = registry.render_prometheus()
    assert 'requests_total{outcome="hit"} 1' in text
    assert 'latency_seconds_bucket{source="raw",le="0.05"} 1' in text
    assert 'latency_seconds_bucket{source="raw",le="0.025"} 0' in text
    assert 'latency_seconds_count{source="raw"} 1' in text


def test_source_kind():
    assert source_kind("pyramid:L12+raw") == "pyramid+raw"
    assert source_kind("pyramid:D3") == "pyramid"


def test_metrics_endpoint(client):
    response = client.get("/metrics")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")


async def test_tiles_paint_in_process_pool(tmp_data_dir, monkeypatch):
    """그리기 프로세스 풀 경로: 결과가 스레드에서 그린 것과 같은 PNG다."""
    from webchart.core import concurrency

    x = np.arange(20_000, dtype=float)
    source = tmp_data_dir / "uploads" / "p.parquet"
    pd.DataFrame({"x": x, "y": np.cos(x / 100)}).to_parquet(source, index=False)
    convert_to_parquet(source, "p")
    meta = get_dataset_meta("p")
    spec = ChartSpec(dataset_id="p", x_axis={"column": "x"}, y_columns=["y"])
    lattice = lattice_for(meta, "x", settings.tile_size)
    plan = plan_frame(meta, spec, lattice.domain, 1, prefetch_radius=0)
    request = TileRequest(meta, spec, lattice, TileAddress(1, 1), 2, plan.y_ranges)

    in_thread = (await PipelineTileRenderer().render(request)).image
    concurrency.shutdown_render_pool()
    monkeypatch.setattr(settings, "paint_processes", 2)
    concurrency.init_render_pool()
    try:
        assert concurrency._paint_pool is not None
        in_process = (await PipelineTileRenderer().render(request)).image
    finally:
        concurrency.shutdown_render_pool()
    assert in_process == in_thread
