"""라인 차트 무왜곡 오라클.

각 테스트는 "서버가 그린 그림 = 원본 전체를 그린 그림"을 검사한다 (계획 문서 8장).
알려진 버그로 아직 실패하는 경우는 `known_bug`로 표시한다 (지금은 없다).
"""

import matplotlib
import numpy as np
import pandas as pd
import pytest

from webchart.application.pyramids import build_pyramid
from webchart.application.windows import lattice_for
from webchart.config import settings
from webchart.models.dataset import ColumnKind
from webchart.services.data.access import get_dataset_meta
from webchart.services.render.matplotlib_renderer import MatplotlibRenderer
from webchart.testing.raster import compare_envelopes, decode_png, ink_mask
from webchart.testing.synthetic import X_COLUMN, SyntheticSpec, generate

from .harness import (
    STROKE_REACH_PX,
    TOLERANCE_PX,
    assert_no_distortion,
    diff_tiles_against_reference,
    ingest,
    line_spec,
)

# 타일 하나(512px)보다 행이 적어 집계가 일어나지 않는 데이터
SMALL = SyntheticSpec(rows=400, series=2, spikes_per_series=3, seed=10)

# 타일 하나에 약 400행이 들어가는 큰 데이터 (집계 발생)
LARGE_ROWS = 200_000


async def test_harness_matches_reference_without_downsampling():
    """하네스 자체 검증: 집계가 없으면 결과와 정답이 같아야 한다.

    이 테스트가 실패하면 다른 오라클 결과도 믿을 수 없다.
    """
    frame = generate(SMALL)
    meta = ingest(frame, "small")
    spec = line_spec("small", SMALL.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=0))


TIME_SMALL = SyntheticSpec(rows=400, series=2, spikes_per_series=3, time_axis=True, seed=11)


@pytest.mark.parametrize(
    "file_format, timezone",
    [
        pytest.param("csv", None, id="csv-naive"),
        pytest.param("csv", "Asia/Seoul", id="csv-offset"),
        pytest.param("parquet", None, id="parquet-naive"),
        pytest.param("parquet", "UTC", id="parquet-utc"),
    ],
)
async def test_time_axis_tiles_match_reference(file_format, timezone):
    """시간 x축(D3 회귀 방지): 문자열·timestamp 어느 쪽으로 들어와도 원본과 같은 그림."""
    frame = generate(TIME_SMALL)
    if timezone is not None:
        frame[X_COLUMN] = frame[X_COLUMN].dt.tz_localize(timezone)
    meta = ingest(frame, "time", file_format)
    x_info = next(c for c in meta.columns if c.name == X_COLUMN)
    assert x_info.kind is ColumnKind.TIME
    spec = line_spec("time", TIME_SMALL.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=0))


@pytest.mark.parametrize(
    "synthetic",
    [
        pytest.param(SyntheticSpec(rows=LARGE_ROWS, spikes_per_series=40, seed=1), id="spikes"),
        pytest.param(SyntheticSpec(rows=LARGE_ROWS, seed=2), id="noise-band"),
        pytest.param(
            SyntheticSpec(rows=LARGE_ROWS, spikes_per_series=20, irregular_x=True, seed=3),
            id="irregular-x",
        ),
    ],
)
async def test_single_series_survives_tile_aggregation(synthetic):
    """D4 회귀 방지: 픽셀 정렬 M4는 스파이크·노이즈 밴드·불규칙 간격 모두 원본과 같다."""
    frame = generate(synthetic)
    meta = ingest(frame, "single")
    spec = line_spec("single", synthetic.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=0))


MULTI = SyntheticSpec(rows=LARGE_ROWS, series=2, spikes_per_series=20, seed=4)


async def test_secondary_series_peaks_survive_tile_aggregation():
    """D2 회귀 방지: 시리즈마다 따로 집계한다."""
    frame = generate(MULTI)
    meta = ingest(frame, "multi")
    spec = line_spec("multi", MULTI.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=0))


async def test_pyramid_tiles_keep_every_series_envelope():
    """D5 회귀 방지: 피라미드에서 읽은 타일도 다시 깎지 않는다."""
    frame = generate(MULTI)
    ingest(frame, "multi-pyramid")
    build_pyramid("multi-pyramid", X_COLUMN, columns=MULTI.y_columns)
    meta = get_dataset_meta("multi-pyramid")  # 피라미드 정보가 기록된 최신 meta
    spec = line_spec("multi-pyramid", MULTI.y_columns)

    for zoom in (0, 2):
        assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=zoom))


async def test_adjacent_tiles_share_y_scale():
    """D1 회귀 방지: 왼쪽 절반은 진폭 1, 오른쪽 절반은 진폭 100인 데이터를 타일 2장으로.

    타일당 행 수가 타일 폭보다 적어 집계가 일어나지 않으므로 y 스케일만 검사한다.
    """
    rows = 800
    x = np.arange(rows, dtype=np.float64)
    amplitude = np.where(x < rows / 2, 1.0, 100.0)
    frame = pd.DataFrame({X_COLUMN: x, "s0": amplitude * np.sin(x / 10.0)})
    meta = ingest(frame, "step")
    spec = line_spec("step", ["s0"])

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=1))


GAPPY = SyntheticSpec(rows=LARGE_ROWS, series=2, spikes_per_series=10, gaps_per_series=8,
                      gap_rows=2_000, seed=5)  # fmt: skip


async def test_missing_runs_break_the_line():
    """P5: 결측 구간을 선으로 잇지 않는다. 원본(matplotlib가 NaN에서 끊음)과 같은 그림."""
    frame = generate(GAPPY)
    meta = ingest(frame, "gappy")
    spec = line_spec("gappy", GAPPY.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=0))
    build_pyramid("gappy", X_COLUMN, columns=GAPPY.y_columns)
    meta = get_dataset_meta("gappy")
    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=1))


async def test_deep_zoom_tiles_read_raw_rows_exactly(monkeypatch):
    """피라미드 최대 레벨보다 깊은 줌은 원본 범위를 읽어 즉석 집계한다 (이웃 행으로 끝까지 연결)."""
    monkeypatch.setattr(settings, "pyramid_max_level", 10)
    synthetic = SyntheticSpec(rows=LARGE_ROWS, series=2, spikes_per_series=200, seed=6)
    frame = generate(synthetic)
    ingest(frame, "deep")
    build_pyramid("deep", X_COLUMN, columns=synthetic.y_columns)
    meta = get_dataset_meta("deep")
    assert meta.pyramids[X_COLUMN].max_level == 10
    spec = line_spec("deep", synthetic.y_columns)

    # 줌 4 → 레벨 13 > 10: 원본 경로. 가운데 타일 3장 (타일 경계의 연결선 포함)
    diff = await diff_tiles_against_reference(frame, meta, spec, zoom=4, tiles=range(7, 10))
    assert_no_distortion(diff)


async def test_high_dpr_tiles_match_reference():
    """dpr 2: 타일이 1024px로 그려지고 레벨이 하나 깊어져도 원본과 같다."""
    frame = generate(MULTI)
    ingest(frame, "retina")
    build_pyramid("retina", X_COLUMN, columns=MULTI.y_columns)
    meta = get_dataset_meta("retina")
    spec = line_spec("retina", MULTI.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=1, dpr=2))


async def test_time_axis_pyramid_tiles_match_reference():
    """시간 x축 피라미드 (Phase 1.3부터 지원)."""
    synthetic = SyntheticSpec(rows=LARGE_ROWS, spikes_per_series=30, time_axis=True, seed=7)
    frame = generate(synthetic)
    ingest(frame, "time-pyramid")
    build_pyramid("time-pyramid", X_COLUMN, columns=synthetic.y_columns)
    meta = get_dataset_meta("time-pyramid")
    spec = line_spec("time-pyramid", synthetic.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=1))


def _static_diff(client, frame: pd.DataFrame, spec) -> tuple:
    response = client.post("/api/v1/charts/render", json=spec.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    with matplotlib.rc_context({"path.simplify": False}):
        expected_png = MatplotlibRenderer().render(spec, frame)
    expected = ink_mask(decode_png(expected_png))
    actual = ink_mask(decode_png(response.content))
    assert expected.shape == actual.shape, "축 라벨 폭이 달라 그림 크기부터 다르다"
    return compare_envelopes(expected, actual, TOLERANCE_PX, STROKE_REACH_PX), response.headers


def test_static_render_secondary_series_peaks_survive(client):
    """정적 렌더(/charts/render)도 모든 시리즈의 피크를 보존한다 (D2, 원본 경로)."""
    frame = generate(MULTI)
    ingest(frame, "static-multi")
    spec = line_spec("static-multi", MULTI.y_columns).model_copy(
        update={"width": 800, "height": 400}
    )

    diff, headers = _static_diff(client, frame, spec)
    assert_no_distortion(diff)
    assert (headers["X-Webchart-Exact"], headers["X-Webchart-Source"]) == ("true", "raw")


def test_static_render_from_pyramid_is_marked_approximate(client, monkeypatch):
    """원본이 예산을 넘으면 피라미드 근사를 쓰고 exact=false로 알린다 (P3).

    근사라도 경계 버킷 하나 이내의 오차라, 피크가 사라지는 수준의 왜곡은 없어야 한다.
    """
    frame = generate(MULTI)
    ingest(frame, "static-pyramid")
    build_pyramid("static-pyramid", X_COLUMN, columns=MULTI.y_columns)
    monkeypatch.setattr(settings, "exact_raw_row_budget", 1_000)
    spec = line_spec("static-pyramid", MULTI.y_columns).model_copy(
        update={"width": 800, "height": 400}
    )

    diff, headers = _static_diff(client, frame, spec)
    assert headers["X-Webchart-Exact"] == "false"
    assert headers["X-Webchart-Source"].startswith("pyramid:L")
    assert diff.max_deviation_px <= 3, diff.summary()


def test_static_render_aligned_with_pyramid_is_exact(client, monkeypatch):
    """요청 격자가 피라미드 레벨과 경계가 맞으면 피라미드에서도 정확하다."""
    frame = generate(MULTI)
    meta = ingest(frame, "static-aligned")
    build_pyramid("static-aligned", X_COLUMN, columns=MULTI.y_columns)
    monkeypatch.setattr(settings, "exact_raw_row_budget", 1_000)
    lattice = lattice_for(meta, X_COLUMN, settings.tile_size)
    # 데이터 영역 폭 = round(0.9·1024) - round(0.125·1024) = 794px... 정렬되지 않으므로
    # 레벨 격자 한 칸 폭의 정수배가 되는 x범위를 직접 고른다
    spec = line_spec("static-aligned", MULTI.y_columns).model_copy(update={"width": 1000})
    width_px, _ = MatplotlibRenderer().plot_area_px(spec)
    level_grid = lattice.level_grid(16)
    x_range = (level_grid.origin, level_grid.origin + width_px * 8 * level_grid.bucket_width)
    spec = spec.model_copy(update={"x_axis": spec.x_axis.model_copy(update={"range": x_range})})

    response = client.post("/api/v1/charts/render", json=spec.model_dump(mode="json"))
    assert response.headers["X-Webchart-Exact"] == "true"
    assert response.headers["X-Webchart-Source"].startswith("pyramid:L")
