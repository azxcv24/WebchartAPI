"""오라클 테스트 하네스.

정답(원본 전체를 그린 그림)과 파이프라인 결과(서버가 실제로 내보내는 그림)를
같은 painter, 같은 격자, 같은 크기로 만들어 비교한다. 무왜곡의 정의와 허용 오차는
docs/improvement-plan.md 8장을 따른다.
"""

import matplotlib
import numpy as np
import pandas as pd
import pytest

from webchart.config import settings
from webchart.domain.geometry import PixelLattice
from webchart.models.chart import AxisConfig, ChartSpec, ChartType
from webchart.models.dataset import DatasetMeta
from webchart.services.ingest.upload import convert_to_parquet
from webchart.services.render import tile_renderer
from webchart.services.render.matplotlib_renderer import MatplotlibRenderer
from webchart.testing.raster import (
    RasterDiff,
    compare_envelopes,
    decode_png,
    ink_mask,
    stitch_horizontally,
)
from webchart.testing.synthetic import X_COLUMN

# 열 외곽선 허용 오차. 안티앨리어싱 경계에서 1px 흔들림은 왜곡으로 보지 않는다.
TOLERANCE_PX = 1

# 허용하는 어긋난 열 수. 무왜곡 계약이므로 0이다.
MAX_MISMATCHED_COLUMNS = 0

# 알려진 왜곡 버그 목록 (docs/improvement-plan.md 1장). 고치면 여기서 지운다.
KNOWN_BUGS = {
    "D1": "타일마다 y축을 자동 스케일해 인접 타일의 스케일이 다르다",
    "D2": "다중 시리즈 다운샘플링이 첫 y컬럼만 기준으로 점을 고른다",
    "D4": "다운샘플 버킷이 행 개수 기준이고 LTTB는 피크 보존을 보장하지 않는다",
    "D5": "타일 경로가 피라미드 envelope에 LTTB를 다시 적용한다",
}


def known_bug(bug_id: str) -> pytest.MarkDecorator:
    """알려진 버그로 실패해야 하는 테스트 표시.

    strict=True라서 버그를 고쳐 테스트가 통과하면 오히려 실패한다. 그때
    표시와 KNOWN_BUGS 항목을 지워 등록부를 최신으로 유지한다.
    """
    return pytest.mark.xfail(strict=True, reason=f"{bug_id}: {KNOWN_BUGS[bug_id]}")


def assert_no_distortion(diff: RasterDiff) -> None:
    assert diff.mismatched_columns <= MAX_MISMATCHED_COLUMNS, diff.summary()


def ingest(frame: pd.DataFrame, dataset_id: str, file_format: str = "parquet") -> DatasetMeta:
    """DataFrame을 파일로 쓴 뒤 실제 업로드 경로(convert_to_parquet)로 인제스트한다."""
    source = settings.data_dir / "uploads" / f"{dataset_id}.{file_format}"
    if file_format == "csv":
        frame.to_csv(source, index=False)
    elif file_format == "parquet":
        frame.to_parquet(source, index=False)
    else:
        raise ValueError(f"지원하지 않는 형식: {file_format}")
    return convert_to_parquet(source, dataset_id)


def line_spec(dataset_id: str, y_columns: list[str]) -> ChartSpec:
    return ChartSpec(
        dataset_id=dataset_id,
        chart_type=ChartType.LINE,
        x_axis=AxisConfig(column=X_COLUMN),
        y_columns=y_columns,
    )


async def render_pipeline_strip(meta: DatasetMeta, spec: ChartSpec, zoom: int) -> np.ndarray:
    """줌 `zoom`의 모든 타일을 실제 타일 경로로 그려 왼쪽부터 이어 붙인다."""
    tile_images = [
        decode_png(await tile_renderer.render_tile(meta.dataset_id, spec, zoom, index, meta=meta))
        for index in range(2**zoom)
    ]
    return stitch_horizontally(tile_images)


def render_reference_strip(
    frame: pd.DataFrame,
    spec: ChartSpec,
    lattice: PixelLattice,
    zoom: int,
) -> np.ndarray:
    """원본 전체를 타일 경로와 같은 크기·x구간으로 한 장에 그린다 (정답).

    matplotlib의 경로 단순화(path.simplify)를 끈다. 단순화도 일종의 다운샘플링이라
    정답 그림에 섞이면 안 된다.
    """
    x_column = spec.x_axis.column
    reference = frame.copy()
    if pd.api.types.is_datetime64_any_dtype(reference[x_column]):
        reference[x_column] = reference[x_column].astype("int64").astype("float64")

    with matplotlib.rc_context({"path.simplify": False}):
        png = MatplotlibRenderer().render_raster(
            reference,
            x_column,
            spec.y_columns,
            spec.chart_type,
            width_px=lattice.tile_size_px * 2**zoom,
            height_px=lattice.tile_size_px,
            x_range=lattice.domain.as_tuple(),
        )
    return decode_png(png)


async def diff_tiles_against_reference(
    frame: pd.DataFrame, meta: DatasetMeta, spec: ChartSpec, zoom: int
) -> RasterDiff:
    """타일 경로 결과를 정답과 비교한다."""
    lattice = tile_renderer.lattice_for(meta, spec.x_axis.column)
    expected = render_reference_strip(frame, spec, lattice, zoom)
    actual = await render_pipeline_strip(meta, spec, zoom)
    return compare_envelopes(ink_mask(expected), ink_mask(actual), TOLERANCE_PX)
