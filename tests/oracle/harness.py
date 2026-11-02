"""오라클 테스트 하네스.

정답(원본 전체를 그린 그림)과 파이프라인 결과(서버가 실제로 내보내는 그림)를
같은 painter, 같은 격자, 같은 크기로 만들어 비교한다. 무왜곡의 정의와 허용 오차는
docs/improvement-plan.md 8장을 따른다.
"""

import matplotlib
import numpy as np
import pandas as pd
import pytest
from matplotlib.figure import Figure

from webchart.application.axes import x_ticks, y_ticks
from webchart.application.tiles import PipelineTileRenderer, TileRequest, plan_frame
from webchart.application.windows import lattice_for
from webchart.config import settings
from webchart.domain.geometry import TileAddress, XRange
from webchart.domain.y_range import YRange
from webchart.models.chart import AxisConfig, ChartSpec, ChartType
from webchart.models.dataset import ColumnInfo, DatasetMeta
from webchart.services.ingest.upload import convert_to_parquet
from webchart.services.render.matplotlib_renderer import AxisTicks, MatplotlibRenderer, raw_drawings
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

# 선 굵기 번짐 허용: 이웃 1열까지 (testing/raster.py, 계획 문서 8장).
# dpr이 2배면 선의 픽셀 굵기도 2배라 허용 열 수도 dpr배로 늘린다.
STROKE_REACH_PX = 1

# 허용하는 어긋난 열 수. 무왜곡 계약이므로 0이다.
MAX_MISMATCHED_COLUMNS = 0

# 알려진 왜곡 버그 목록 (docs/improvement-plan.md 1장). 고치면 여기서 지운다.
# Phase 1에서 D1·D2·D4·D5를 모두 고쳐 지금은 비어 있다.
KNOWN_BUGS: dict[str, str] = {}


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


async def render_pipeline_strip(
    meta: DatasetMeta, spec: ChartSpec, zoom: int, tiles: range | None = None, dpr: int = 1
) -> np.ndarray:
    """줌 `zoom`의 타일들(기본: 전부)을 실제 타일 경로로 그려 왼쪽부터 잇는다.

    y범위는 프레임 계획(plan_frame)이 정한 값을 모든 타일이 공유한다 (실제 WS 경로와 같다).
    """
    lattice = lattice_for(meta, spec.x_axis.column, settings.tile_size)
    plan = plan_frame(meta, spec, lattice.domain, zoom, dpr=dpr, prefetch_radius=0)
    renderer = PipelineTileRenderer()
    tile_images = []
    for index in tiles or range(lattice.tile_count(zoom)):
        tile = TileAddress(zoom, index)
        request = TileRequest(meta, spec, plan.lattice, tile, dpr, plan.y_ranges)
        tile_images.append(decode_png((await renderer.render(request)).image))
    return stitch_horizontally(tile_images)


def render_reference_strip(
    frame: pd.DataFrame, spec: ChartSpec, x_range: XRange, width_px: int, height_px: int, dpr: int
) -> np.ndarray:
    """원본 전체를 주어진 x구간·크기로 한 장에 그린다 (정답).

    집계 없이 모든 행을 그리고, y축은 matplotlib 자동 스케일에 맡긴다(구간 밖 행 포함 전체
    데이터 기준). 파이프라인 코드(집계, y범위 정책)를 거치지 않아야 정답으로서 독립적이다.
    """
    with matplotlib.rc_context({"path.simplify": False}):
        png = MatplotlibRenderer().render_raster(
            raw_drawings(spec, frame), x_range, None, width_px, height_px, dpr=dpr
        )
    return decode_png(png)


def render_reference_chart(spec: ChartSpec, frame: pd.DataFrame, x_info: ColumnInfo) -> bytes:
    """정적 렌더의 정답: 원본 전체를 matplotlib 자동 스케일로 그린 완성 차트.

    눈금만 서버 눈금 서비스로 붙인다 (matplotlib 기본 눈금과 라벨이 달라 그림 크기가
    달라지는 것을 막기 위해서다). 축 범위는 matplotlib이 원본으로 정한 값을 읽어 쓴다.
    """
    painter = MatplotlibRenderer()
    drawings = raw_drawings(spec, frame)
    probe = Figure(figsize=(spec.width / 100, spec.height / 100))
    probe_axes = probe.add_subplot(111)
    for drawing in drawings:
        probe_axes.plot(drawing.shape.x, drawing.shape.y)
    if spec.x_axis.range is not None:
        probe_axes.set_xlim(*spec.x_axis.range)
    view = XRange(*probe_axes.get_xlim())
    y_range = YRange(*probe_axes.get_ylim())
    width_px, height_px = painter.plot_area_px(spec)
    ticks = AxisTicks(x=x_ticks(spec, x_info, view, width_px), y={"y": y_ticks(y_range, height_px)})
    with matplotlib.rc_context({"path.simplify": False}):
        return painter.render_chart(spec, drawings, None, None, ticks)


async def diff_tiles_against_reference(
    frame: pd.DataFrame,
    meta: DatasetMeta,
    spec: ChartSpec,
    zoom: int,
    tiles: range | None = None,
    dpr: int = 1,
) -> RasterDiff:
    """타일 경로 결과를 같은 구간의 정답과 비교한다."""
    lattice = lattice_for(meta, spec.x_axis.column, settings.tile_size)
    tiles = tiles or range(lattice.tile_count(zoom))
    first = lattice.tile_grid(TileAddress(zoom, tiles[0]), dpr)
    last = lattice.tile_grid(TileAddress(zoom, tiles[-1]), dpr)
    size_px = lattice.tile_size_px * dpr
    expected = render_reference_strip(
        frame, spec, XRange(first.origin, last.end), size_px * len(tiles), size_px, dpr
    )
    actual = await render_pipeline_strip(meta, spec, zoom, tiles, dpr)
    return compare_envelopes(
        ink_mask(expected), ink_mask(actual), TOLERANCE_PX, STROKE_REACH_PX * dpr
    )
