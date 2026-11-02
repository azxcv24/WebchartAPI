"""band·bar·candlestick·밀도 레이어 오라클 (계획 문서 8장).

정의
- band / candlestick: 픽셀 열(또는 캔들 구간)별 min·max(·first·last)가 원본과 같다
- bar: 막대 값 = 그 x 구간 원본 행의 집계값 (부동소수 오차 허용)
- density: 픽셀별 점 개수가 원본을 같은 경계로 센 값과 같다

정답은 pandas/numpy로 원본 행을 직접 묶어 계산한다 (파이프라인 코드를 쓰지 않는다).
원본 경로와 피라미드 경로를 모두 검사한다.
"""

import numpy as np
import pandas as pd
import pytest

from webchart.application.pyramids import build_pyramid
from webchart.application.tiles import (
    PipelineTileRenderer,
    TileRequest,
    plan_frame,
    tile_drawings,
)
from webchart.application.windows import lattice_for
from webchart.config import settings
from webchart.domain.geometry import BucketGrid, TileAddress
from webchart.domain.projection import Band, Bars, Candles, DensityGrid
from webchart.models.chart import AxisConfig, ChartSpec, LayerSpec, OhlcColumns
from webchart.services.data.access import get_dataset_meta
from webchart.services.data.x_domain import to_canonical
from webchart.services.render.matplotlib_renderer import MatplotlibRenderer
from webchart.testing.raster import compare_envelopes, decode_png, ink_mask, stitch_horizontally
from webchart.testing.synthetic import X_COLUMN, SyntheticSpec, generate

from .harness import ingest

DATA = SyntheticSpec(rows=200_000, series=2, spikes_per_series=30, gaps_per_series=4, seed=21)


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return generate(DATA)


def _spec(dataset_id: str, *layers: LayerSpec) -> ChartSpec:
    return ChartSpec(dataset_id=dataset_id, x_axis=AxisConfig(column=X_COLUMN), layers=list(layers))


def _naive_groups(frame: pd.DataFrame, column: str, grid: BucketGrid, group: int = 1):
    """원본 행을 격자 구간(group열씩)으로 직접 묶는다 (정답)."""
    x = to_canonical(frame[X_COLUMN])
    inside = (x >= grid.origin) & (x < grid.end)
    index = np.floor((x[inside] - grid.origin) / grid.bucket_width).astype(int) // group
    values = frame[column].to_numpy()[inside]
    data = pd.DataFrame({"g": index, "x": x[inside], "y": values}).dropna(subset=["y"])
    return data.sort_values(["g", "x"], kind="stable").groupby("g")


def _tile(meta, spec, zoom=0, index=0, dpr=1):
    lattice = lattice_for(meta, X_COLUMN, settings.tile_size)
    tile = TileAddress(zoom, index)
    plan = plan_frame(meta, spec, lattice.tile_range(tile), zoom, dpr=dpr, prefetch_radius=0)
    request = TileRequest(meta, spec, plan.lattice, tile, dpr, plan.y_ranges)
    return request, tile_drawings(request), lattice.tile_grid(tile, dpr)


@pytest.mark.parametrize("with_pyramid", [False, True], ids=["raw", "pyramid"])
async def test_band_matches_per_column_extremes(frame, with_pyramid):
    ingest(frame, "band")
    if with_pyramid:
        build_pyramid("band", X_COLUMN, columns=DATA.y_columns)
    meta = get_dataset_meta("band")
    spec = _spec("band", LayerSpec(kind="band", column="s1"))

    _, drawn, grid = _tile(meta, spec, zoom=1, index=1)
    band = drawn.drawings[0].shape
    assert isinstance(band, Band) and drawn.is_exact
    groups = _naive_groups(frame, "s1", grid)
    expected = groups["y"].agg(["min", "max"])
    columns = np.round((band.left_x - grid.origin) / grid.bucket_width).astype(int)

    assert list(columns) == list(expected.index)
    np.testing.assert_array_equal(band.low, expected["min"].to_numpy())
    np.testing.assert_array_equal(band.high, expected["max"].to_numpy())


@pytest.mark.parametrize("agg", ["sum", "mean", "count", "min", "max"])
async def test_bar_values_equal_exact_aggregate(frame, agg):
    ingest(frame, "bars")
    build_pyramid("bars", X_COLUMN, columns=DATA.y_columns)
    meta = get_dataset_meta("bars")
    spec = _spec("bars", LayerSpec(kind="bar", column="s0", agg=agg, group_px=16))

    _, drawn, grid = _tile(meta, spec, zoom=0)
    bars = drawn.drawings[0].shape
    assert isinstance(bars, Bars) and drawn.source.startswith("pyramid:")
    expected = _naive_groups(frame, "s0", grid, group=16)["y"].agg(agg)

    np.testing.assert_allclose(bars.value, expected.to_numpy(), rtol=1e-9)
    assert len(bars) == 512 // 16


async def test_price_candles_are_first_max_min_last(frame):
    ingest(frame, "candles")
    meta = get_dataset_meta("candles")
    spec = _spec("candles", LayerSpec(kind="candlestick", column="s0", group_px=8))

    _, drawn, grid = _tile(meta, spec, zoom=2, index=2)
    candles = drawn.drawings[0].shape
    assert isinstance(candles, Candles)
    groups = _naive_groups(frame, "s0", grid, group=8)["y"]

    np.testing.assert_array_equal(candles.open, groups.first().to_numpy())
    np.testing.assert_array_equal(candles.high, groups.max().to_numpy())
    np.testing.assert_array_equal(candles.low, groups.min().to_numpy())
    np.testing.assert_array_equal(candles.close, groups.last().to_numpy())


async def test_prebuilt_ohlc_columns_merge_correctly():
    rows = 50_000
    rng = np.random.default_rng(3)
    close = np.cumsum(rng.normal(0, 1, rows))
    open_ = np.roll(close, 1)
    high = np.maximum(open_, close) + rng.random(rows)
    low = np.minimum(open_, close) - rng.random(rows)
    ohlc = pd.DataFrame({X_COLUMN: np.arange(rows, dtype=float), "o": open_, "h": high,
                         "l": low, "c": close})  # fmt: skip
    ingest(ohlc, "ohlc")
    build_pyramid("ohlc", X_COLUMN)
    meta = get_dataset_meta("ohlc")
    layer = LayerSpec(kind="candlestick", ohlc=OhlcColumns(open="o", high="h", low="l", close="c"))

    _, drawn, grid = _tile(meta, _spec("ohlc", layer), zoom=0)
    candles = drawn.drawings[0].shape
    assert isinstance(candles, Candles)
    opens = _naive_groups(ohlc, "o", grid, 8)["y"].first()
    highs = _naive_groups(ohlc, "h", grid, 8)["y"].max()
    lows = _naive_groups(ohlc, "l", grid, 8)["y"].min()
    closes = _naive_groups(ohlc, "c", grid, 8)["y"].last()

    for actual, expected in ((candles.open, opens), (candles.high, highs),
                             (candles.low, lows), (candles.close, closes)):  # fmt: skip
        np.testing.assert_array_equal(actual, expected.to_numpy())


@pytest.mark.parametrize("with_pyramid", [False, True], ids=["raw", "pyramid"])
@pytest.mark.parametrize("dpr", [1, 2])
async def test_density_counts_equal_raw_binning(frame, with_pyramid, dpr):
    ingest(frame, "density")
    if with_pyramid:
        build_pyramid("density", X_COLUMN, columns=DATA.y_columns)
    meta = get_dataset_meta("density")
    spec = _spec("density", LayerSpec(kind="heatmap", column="s0"))

    request, drawn, grid = _tile(meta, spec, zoom=1, index=0, dpr=dpr)
    density = drawn.drawings[0].shape
    assert isinstance(density, DensityGrid) and drawn.is_exact
    if with_pyramid:
        assert drawn.source.startswith("pyramid:D")

    y_range = request.y_ranges["y"]
    x = to_canonical(frame[X_COLUMN])
    y = frame["s0"].to_numpy()
    row_height = (y_range.high - y_range.low) / density.rows
    in_x = (x >= grid.origin) & (x < grid.end) & ~np.isnan(y)
    column = np.floor((x[in_x] - grid.origin) / grid.bucket_width).astype(int)
    row = np.minimum(np.floor((y[in_x] - y_range.low) / row_height).astype(int), density.rows - 1)
    expected = np.zeros_like(density.counts)
    np.add.at(expected, (row, column), 1)

    np.testing.assert_array_equal(density.counts, expected)
    assert density.counts.sum() == np.count_nonzero(in_x)  # 점 하나도 빠지지 않는다


async def test_tiles_stitch_into_one_strip_for_every_layer(frame):
    """타일 경계의 이음새: 타일 4장을 이은 그림 = 같은 격자로 한 장에 그린 그림."""
    ingest(frame, "seams")
    build_pyramid("seams", X_COLUMN, columns=DATA.y_columns)
    meta = get_dataset_meta("seams")
    spec = _spec(
        "seams",
        LayerSpec(kind="band", column="s1"),
        LayerSpec(kind="bar", column="s0", agg="mean"),
        LayerSpec(kind="line", column="s0"),
    )
    zoom = 2
    lattice = lattice_for(meta, X_COLUMN, settings.tile_size)
    plan = plan_frame(meta, spec, lattice.domain, zoom, prefetch_radius=0)
    renderer = PipelineTileRenderer()
    tiles = []
    for index in range(4):
        request = TileRequest(meta, spec, lattice, TileAddress(zoom, index), 1, plan.y_ranges)
        tiles.append(decode_png((await renderer.render(request)).image))

    # 한 장: 타일마다 투영한 도형을 모아 4배 폭 그림 하나에 그린다
    drawings_by_layer: list[list] = [[] for _ in spec.layers]
    for index in range(4):
        request = TileRequest(meta, spec, lattice, TileAddress(zoom, index), 1, plan.y_ranges)
        for layer_index, drawing in enumerate(tile_drawings(request).drawings):
            drawings_by_layer[layer_index].append(drawing)
    merged = [_merge_drawings(parts) for parts in drawings_by_layer]
    whole = decode_png(
        MatplotlibRenderer().render_raster(
            merged, lattice.domain, plan.y_ranges, 4 * settings.tile_size, settings.tile_size
        )
    )
    diff = compare_envelopes(ink_mask(whole), ink_mask(stitch_horizontally(tiles)), 1, 1)
    assert diff.mismatched_columns == 0, diff.summary()


def _merge_drawings(parts):
    from dataclasses import fields, replace

    first = parts[0]
    shape = first.shape
    merged_fields = {
        f.name: np.concatenate([getattr(p.shape, f.name) for p in parts]) for f in fields(shape)
    }
    return replace(first, shape=type(shape)(**merged_fields))


async def test_dual_axis_ranges_are_independent(frame):
    """y2축 레이어는 자기 범위를 쓴다. 막대(합계)는 0을 포함한 집계값 범위."""
    ingest(frame, "dual")
    meta = get_dataset_meta("dual")
    spec = _spec(
        "dual",
        LayerSpec(kind="line", column="s0"),
        LayerSpec(kind="bar", column="s1", agg="count", axis="y2"),
    )

    request, drawn, _ = _tile(meta, spec, zoom=0)
    y, y2 = request.y_ranges["y"], request.y_ranges["y2"]
    s0 = frame["s0"]
    assert (y.low, y.high) == pytest.approx(
        (s0.min() - 0.05 * (s0.max() - s0.min()), s0.max() + 0.05 * (s0.max() - s0.min()))
    )
    counts = drawn.drawings[1].shape.value
    assert y2.low <= 0 <= y2.high and y2.high >= counts.max()
