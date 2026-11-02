"""라인 차트 무왜곡 오라클.

각 테스트는 "서버가 그린 그림 = 원본 전체를 그린 그림"을 검사한다.
알려진 버그로 아직 실패하는 경우는 `known_bug`로 표시한다.
"""

import matplotlib
import numpy as np
import pandas as pd
import pytest

from webchart.models.dataset import ColumnKind
from webchart.services.data.pyramid import build_pyramid
from webchart.services.render.matplotlib_renderer import MatplotlibRenderer
from webchart.testing.raster import compare_envelopes, decode_png, ink_mask
from webchart.testing.synthetic import X_COLUMN, SyntheticSpec, generate

from .harness import (
    STROKE_REACH_PX,
    TOLERANCE_PX,
    assert_no_distortion,
    diff_tiles_against_reference,
    ingest,
    known_bug,
    line_spec,
)

# 타일 하나(512px)보다 행이 적어 다운샘플링이 일어나지 않는 데이터
SMALL = SyntheticSpec(rows=400, series=2, spikes_per_series=3, seed=10)

# 타일 하나에 약 400행이 들어가는 큰 데이터 (다운샘플링 발생)
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


@known_bug("D4")
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
async def test_single_series_survives_tile_downsampling(synthetic):
    frame = generate(synthetic)
    meta = ingest(frame, "single")
    spec = line_spec("single", synthetic.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=0))


MULTI = SyntheticSpec(rows=LARGE_ROWS, series=2, spikes_per_series=20, seed=4)


@known_bug("D2")
async def test_secondary_series_peaks_survive_tile_downsampling():
    frame = generate(MULTI)
    meta = ingest(frame, "multi")
    spec = line_spec("multi", MULTI.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=0))


@known_bug("D5")
async def test_pyramid_tiles_keep_every_series_envelope():
    frame = generate(MULTI)
    ingest(frame, "multi-pyramid")
    build_pyramid("multi-pyramid", X_COLUMN, columns=MULTI.y_columns)
    # 피라미드 정보가 기록된 최신 meta로 렌더해야 피라미드 경로를 탄다
    from webchart.services.data.access import get_dataset_meta

    meta = get_dataset_meta("multi-pyramid")
    spec = line_spec("multi-pyramid", MULTI.y_columns)

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=0))


@known_bug("D1")
async def test_adjacent_tiles_share_y_scale():
    """왼쪽 절반은 진폭 1, 오른쪽 절반은 진폭 100인 데이터를 줌 1(타일 2장)로 그린다.

    타일당 행 수가 타일 폭보다 적어 다운샘플링이 일어나지 않으므로 y 스케일만 검사한다.
    """
    rows = 800
    x = np.arange(rows, dtype=np.float64)
    amplitude = np.where(x < rows / 2, 1.0, 100.0)
    frame = pd.DataFrame({X_COLUMN: x, "s0": amplitude * np.sin(x / 10.0)})
    meta = ingest(frame, "step")
    spec = line_spec("step", ["s0"])

    assert_no_distortion(await diff_tiles_against_reference(frame, meta, spec, zoom=1))


@known_bug("D2")
def test_static_render_secondary_series_peaks_survive(client):
    """정적 렌더(/charts/render)도 모든 시리즈의 피크를 보존해야 한다."""
    frame = generate(MULTI)
    ingest(frame, "static-multi")
    spec = line_spec("static-multi", MULTI.y_columns).model_copy(
        update={"width": 800, "height": 400}
    )

    response = client.post("/api/v1/charts/render", json=spec.model_dump(mode="json"))
    assert response.status_code == 200
    with matplotlib.rc_context({"path.simplify": False}):
        expected_png = MatplotlibRenderer().render(spec, frame)

    expected = ink_mask(decode_png(expected_png))
    actual = ink_mask(decode_png(response.content))
    assert expected.shape == actual.shape, "축 라벨 폭이 달라 그림 크기부터 다르다"
    assert_no_distortion(compare_envelopes(expected, actual, TOLERANCE_PX, STROKE_REACH_PX))
