"""정적 렌더의 피라미드 재묶기 오라클 (application/view_regroup.py).

정의: 임의의 x범위·폭(픽셀 열 경계가 피라미드 버킷과 맞지 않는 격자)에서, 피라미드로 다시
묶은 열별 집계가 원본을 같은 격자로 직접 집계한 값(RawStore.aggregate)과 같다.
- count, first/last, min/max(값과 x), 결측 위치는 완전히 같다. 합은 부동소수 오차만 허용한다.
- 격자 밖 이웃은 선이 이어지는 점(가장 가까운 행의 위치, 결측 여부, 값)이 같다.

최상세 버킷이 촘촘한 피라미드(경계에 걸친 버킷이 많아 원본으로 나눈다)와 기본 피라미드를
모두 검사한다.
"""

import numpy as np
import pytest

from webchart.application.pyramids import build_pyramid
from webchart.application.view_regroup import regroup_view
from webchart.application.windows import fetch_view_windows, usable_pyramid
from webchart.config import settings
from webchart.domain.geometry import BucketGrid
from webchart.services.data.access import get_dataset_meta, raw_store
from webchart.testing.synthetic import X_COLUMN, SyntheticSpec, generate

from .harness import ingest

DATA = SyntheticSpec(
    rows=60_000, series=2, spikes_per_series=25, gaps_per_series=5, irregular_x=True, seed=31
)
EXACT_FIELDS = ("count", "first_x", "first_y", "last_x", "last_y", "min_x", "min_y", "max_x",
                "max_y", "gap_first_x", "gap_last_x")  # fmt: skip


@pytest.fixture(params=["coarse-finest", "default-finest"])
def meta(request, monkeypatch):
    """coarse: 최상세 레벨을 낮춰 버킷 하나에 행이 수백 개. default: 기본 선택."""
    monkeypatch.setattr(settings, "ingest_row_group_rows", 4_096)  # 로우그룹 여러 개
    dataset_id = f"regroup-{request.param}"
    ingest(generate(DATA), dataset_id)
    if request.param == "coarse-finest":
        monkeypatch.setattr(settings, "pyramid_max_level", 8)
    build_pyramid(dataset_id, X_COLUMN, columns=DATA.y_columns)
    return get_dataset_meta(dataset_id)


def _grids(meta, count: int, seed: int) -> list[BucketGrid]:
    """전체(양옆 여백 포함)와 임의 구간들. 경계가 피라미드와 맞지 않는 폭을 쓴다."""
    column = next(c for c in meta.columns if c.name == X_COLUMN)
    low, high = column.min_numeric, column.max_numeric
    rng = np.random.default_rng(seed)
    grids = [BucketGrid(low - 3.7, (high - low + 9.1) / 797, 797)]
    for _ in range(count):
        start, end = np.sort(rng.uniform(low - 50, high + 50, 2))
        width_px = int(rng.integers(40, 1500))
        grids.append(BucketGrid(float(start), float(end - start) / width_px, width_px))
    return grids


def _nearest_row(edge, side: str) -> tuple:
    """격자 밖 이웃에서 선이 이어지는 행: (x, 결측인지, 값)."""
    if len(edge) == 0:
        return ()
    if side == "lead":
        x = max(edge.last_x[0], edge.gap_last_x[0])
        is_gap = edge.gap_last_x[0] > edge.last_x[0]
        return (x, is_gap, None if is_gap else edge.last_y[0])
    x = min(edge.first_x[0], edge.gap_first_x[0])
    is_gap = edge.gap_first_x[0] < edge.first_x[0]
    return (x, is_gap, None if is_gap else edge.first_y[0])


def _assert_same(actual, expected, label: str) -> None:
    for name in EXACT_FIELDS:
        np.testing.assert_array_equal(getattr(actual.stats, name), getattr(expected.stats, name),
                                      f"{label}: {name}")  # fmt: skip
    np.testing.assert_allclose(actual.stats.sum_y, expected.stats.sum_y, rtol=1e-9, atol=1e-6)
    for side in ("lead", "trail"):
        got = _nearest_row(getattr(actual, side), side)
        want = _nearest_row(getattr(expected, side), side)
        assert got == want, f"{label}: {side}"


def test_regrouped_columns_equal_raw_aggregate(meta):
    reader = usable_pyramid(meta, X_COLUMN, DATA.y_columns)
    store = raw_store(meta)
    for index, grid in enumerate(_grids(meta, count=12, seed=5)):
        result = regroup_view(reader, store, X_COLUMN, DATA.y_columns, grid, raw_row_budget=10**9)
        expected = store.aggregate(X_COLUMN, DATA.y_columns, grid)
        assert result.is_exact, f"grid {index}"
        for name in DATA.y_columns:
            _assert_same(result.windows[name], expected[name], f"grid {index} {name}")


def test_coarse_pyramid_reads_raw_only_for_straddling_buckets(meta):
    """원본은 경계에 걸친 버킷이 든 로우그룹만 읽는다 (구간 전체보다 적다)."""
    if meta.pyramids[X_COLUMN].max_level != 8:
        pytest.skip("촘촘한 피라미드에서만 의미가 있다")
    reader = usable_pyramid(meta, X_COLUMN, DATA.y_columns)
    store = raw_store(meta)
    grid = _grids(meta, count=0, seed=0)[0]
    reads: list[int] = []
    original = store.read_groups

    def counting(x_column, y_columns, groups):
        reads.append(len(groups))
        return original(x_column, y_columns, groups)

    store.read_groups = counting
    try:
        result = regroup_view(reader, store, X_COLUMN, DATA.y_columns, grid, raw_row_budget=10**9)
    finally:
        del store.read_groups
    assert result.is_exact and result.source == "pyramid:L8+raw"
    assert reads and reads[0] <= len(store.zone_map(X_COLUMN))


def test_over_budget_falls_back_to_marked_approximation(meta):
    """걸친 버킷의 원본이 예산을 넘으면 exact=false로 표시하고, 행 수는 그대로 보존한다."""
    reader = usable_pyramid(meta, X_COLUMN, DATA.y_columns)
    store = raw_store(meta)
    grid = _grids(meta, count=0, seed=0)[0]
    result = regroup_view(reader, store, X_COLUMN, DATA.y_columns, grid, raw_row_budget=0)
    expected = store.aggregate(X_COLUMN, DATA.y_columns, grid)
    assert not result.source.endswith("+raw")
    if meta.pyramids[X_COLUMN].max_level == 8:  # 버킷마다 수백 행이라 반드시 걸친다
        assert not result.is_exact
    for name in DATA.y_columns:
        # 열 사이로 몇 행이 옮겨 갈 수는 있어도 사라지거나 생기지 않는다
        assert result.windows[name].stats.count.sum() == expected[name].stats.count.sum()


def test_static_view_path_uses_exact_regroup(meta, monkeypatch):
    """원본 예산을 넘는 정적 렌더 격자는 피라미드 재묶기로 가고, 정확하다고 표시된다."""
    monkeypatch.setattr(settings, "exact_raw_row_budget", 30_000)  # 전체(6만 행)는 넘는다
    grid = _grids(meta, count=0, seed=0)[0]
    windows = fetch_view_windows(meta, X_COLUMN, DATA.y_columns, grid)
    assert windows.source.startswith("pyramid:L")
    if windows.is_exact:
        expected = raw_store(meta).aggregate(X_COLUMN, DATA.y_columns, grid)
        for name in DATA.y_columns:
            _assert_same(windows.windows[name], expected[name], name)
