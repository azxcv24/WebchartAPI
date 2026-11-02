"""M4 투영 테스트: 원본 꺾은선과 같은 그림이 나오는지, 결측에서 끊기는지."""

import numpy as np
import pytest

from webchart.domain.bucket_stats import BucketStats, aggregate_sorted, merge_into
from webchart.domain.geometry import BucketGrid, XRange
from webchart.domain.projection import SeriesWindow, m4_polyline
from webchart.testing.raster import compare_envelopes, decode_png, ink_mask


def _window(x: np.ndarray, y: np.ndarray, grid: BucketGrid) -> SeriesWindow:
    index = grid.index_of(x)
    ids, stats = aggregate_sorted(x, y, index)
    dense = BucketStats.empty(grid.bucket_count)
    inside = (ids >= 0) & (ids < grid.bucket_count)
    merge_into(dense, ids[inside], stats.take(np.flatnonzero(inside)))
    return SeriesWindow(grid, dense, BucketStats.empty(0), BucketStats.empty(0))


def _render(x: np.ndarray, y: np.ndarray, grid: BucketGrid, y_limits) -> np.ndarray:
    import io

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    with matplotlib.rc_context({"path.simplify": False}):
        figure = Figure(figsize=(grid.bucket_count / 100, 2), dpi=100)
        FigureCanvasAgg(figure)
        axes = figure.add_axes((0, 0, 1, 1))
        axes.set_axis_off()
        axes.plot(x, y, linewidth=1.5)
        axes.set_xlim(grid.origin, grid.end)
        axes.set_ylim(*y_limits)
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png")
    return ink_mask(decode_png(buffer.getvalue()))


@pytest.mark.parametrize("seed", range(3))
def test_m4_raster_equals_raw_raster(seed):
    """M4 정리: 버킷 = 픽셀 열이면 그림이 같다.

    허용 오차: 세로 1px(안티앨리어싱), 가로 1열(선 굵기 번짐, testing/raster.py 참고).
    """
    rng = np.random.default_rng(seed)
    x = np.sort(rng.uniform(0, 1, 100_000))
    y = np.cumsum(rng.normal(0, 1, len(x)))
    y[rng.choice(len(x), 20, replace=False)] += 300  # 단일 스파이크
    grid = BucketGrid.covering(XRange(0.0, 1.0), 400)

    polyline = m4_polyline(_window(x, y, grid))
    limits = (y.min() - 10, y.max() + 10)

    assert polyline.point_count <= 4 * grid.bucket_count
    expected = _render(x, y, grid, limits)
    actual = _render(polyline.x, polyline.y, grid, limits)
    diff = compare_envelopes(expected, actual, tolerance_px=1, reach_px=1)
    assert diff.mismatched_columns == 0, diff.summary()


def test_gap_between_buckets_breaks_line():
    x = np.arange(10, dtype=float)
    y = np.array([1, 1, 1, np.nan, np.nan, np.nan, 2, 2, 2, 2], dtype=float)
    grid = BucketGrid(0.0, 1.0, 10)

    polyline = m4_polyline(_window(x, y, grid))

    nan_at = np.flatnonzero(np.isnan(polyline.x))
    assert len(nan_at) == 1
    assert polyline.x[nan_at[0] - 1] == 2.0  # 끊기 전 마지막 점
    assert polyline.x[nan_at[0] + 1] == 6.0  # 끊긴 뒤 첫 점


def test_gap_inside_bucket_before_first_point_breaks_line():
    x = np.array([0.0, 1.2, 1.5])
    y = np.array([1.0, np.nan, 3.0])  # 버킷 1의 첫 유효 점(1.5) 앞에 결측
    polyline = m4_polyline(_window(x, y, BucketGrid(0.0, 1.0, 2)))
    assert np.isnan(polyline.x).sum() == 1


def test_neighbors_extend_line_to_edges():
    grid = BucketGrid(10.0, 1.0, 4)
    inside_ids, inside = aggregate_sorted(
        np.array([10.5, 12.5]), np.array([1.0, 2.0]), np.array([0, 2])
    )
    stats = BucketStats.empty(4)
    merge_into(stats, inside_ids, inside)
    lead = BucketStats.from_points(np.array([3.0]), np.array([9.0]))
    trail = BucketStats.from_points(np.array([20.0]), np.array([7.0]))

    polyline = m4_polyline(SeriesWindow(grid, stats, lead, trail))

    assert polyline.x[0] == 3.0 and polyline.x[-1] == 20.0
    assert not np.isnan(polyline.x).any()


def test_empty_window():
    grid = BucketGrid(0.0, 1.0, 4)
    window = SeriesWindow(grid, BucketStats.empty(4), BucketStats.empty(0), BucketStats.empty(0))
    assert len(m4_polyline(window)) == 0
