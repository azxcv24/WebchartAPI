"""BucketStats 모노이드 성질과 벡터 커널 테스트."""

import numpy as np
import pytest

from webchart.domain.bucket_stats import BucketStats, aggregate_sorted, merge_into


def _random_points(seed: int, size: int = 2_000, nan_ratio: float = 0.05):
    rng = np.random.default_rng(seed)
    x = np.sort(rng.uniform(0, 100, size))
    y = rng.normal(0, 10, size)
    y[rng.random(size) < nan_ratio] = np.nan
    return x, y


def _naive_stats(x: np.ndarray, y: np.ndarray) -> dict:
    """파이썬으로 직접 계산한 기대값 (정렬된 점 하나의 버킷)."""
    valid = ~np.isnan(y)
    vx, vy = x[valid], y[valid]
    gap_x = x[~valid]
    if len(vx) == 0:
        return {"count": 0, "gap_first_x": gap_x.min() if len(gap_x) else np.inf}
    return {
        "count": len(vx),
        "sum_y": vy.sum(),
        "first": (vx[0], vy[0]),
        "last": (vx[-1], vy[-1]),
        "min": (vx[np.argmin(vy)], vy.min()),
        "max": (vx[np.argmax(vy)], vy.max()),
        "gap_first_x": gap_x.min() if len(gap_x) else np.inf,
        "gap_last_x": gap_x.max() if len(gap_x) else -np.inf,
    }


def _assert_bucket_matches(stats: BucketStats, i: int, expected: dict) -> None:
    assert stats.count[i] == expected["count"]
    assert stats.gap_first_x[i] == expected["gap_first_x"]
    if expected["count"] == 0:
        return
    assert stats.sum_y[i] == pytest.approx(expected["sum_y"])
    assert (stats.first_x[i], stats.first_y[i]) == expected["first"]
    assert (stats.last_x[i], stats.last_y[i]) == expected["last"]
    assert (stats.min_x[i], stats.min_y[i]) == expected["min"]
    assert (stats.max_x[i], stats.max_y[i]) == expected["max"]
    assert stats.gap_last_x[i] == expected["gap_last_x"]


@pytest.mark.parametrize("seed", range(5))
def test_aggregate_sorted_matches_naive_per_bucket(seed):
    x, y = _random_points(seed)
    bucket_index = np.floor(x / 7.0).astype(np.int64)

    ids, stats = aggregate_sorted(x, y, bucket_index)

    assert list(ids) == sorted(set(bucket_index))
    for i, bucket in enumerate(ids):
        in_bucket = bucket_index == bucket
        _assert_bucket_matches(stats, i, _naive_stats(x[in_bucket], y[in_bucket]))


@pytest.mark.parametrize("seed", range(5))
def test_merge_is_associative_and_matches_whole(seed):
    """세 조각을 어떤 순서로 묶어 합쳐도 전체를 한 번에 집계한 것과 같다."""
    x, y = _random_points(seed, size=900)
    pieces = [BucketStats.from_points(x[i::3], y[i::3]).regroup(np.array([0])) for i in range(3)]
    whole = BucketStats.from_points(x, y).regroup(np.array([0]))

    left_first = pieces[0].merge(pieces[1]).merge(pieces[2])
    right_first = pieces[0].merge(pieces[1].merge(pieces[2]))

    for merged in (left_first, right_first):
        _assert_bucket_matches(merged, 0, _naive_stats(x, y))
        assert merged.count[0] == whole.count[0]


def test_merge_ignores_empty_buckets():
    x, y = _random_points(0, size=50)
    filled = BucketStats.from_points(x, y).regroup(np.array([0]))
    empty = BucketStats.empty(1)

    for merged in (filled.merge(empty), empty.merge(filled)):
        _assert_bucket_matches(merged, 0, _naive_stats(x, y))


def test_merge_pairs_equals_aggregating_at_coarser_level():
    """레벨 L 두 칸을 합친 결과 = 레벨 L-1에서 직접 집계한 결과 (피라미드 정확성)."""
    x, y = _random_points(7, size=5_000)
    fine_ids, fine = aggregate_sorted(x, y, np.floor(x / 100 * 64).astype(np.int64))
    dense_fine = BucketStats.empty(64)
    merge_into(dense_fine, fine_ids, fine)

    coarse_ids, coarse = aggregate_sorted(x, y, np.floor(x / 100 * 32).astype(np.int64))
    dense_coarse = BucketStats.empty(32)
    merge_into(dense_coarse, coarse_ids, coarse)

    merged = dense_fine.merge_pairs()
    for name in ("count", "first_x", "last_x", "min_x", "min_y", "max_x", "max_y"):
        np.testing.assert_array_equal(getattr(merged, name), getattr(dense_coarse, name), name)
    np.testing.assert_allclose(merged.sum_y, dense_coarse.sum_y)


def test_merge_into_combines_unsorted_batches():
    """정렬되지 않은 순서로 들어온 조각을 합쳐도 결과가 같다 (x로 first/last를 고른다)."""
    x, y = _random_points(3, size=3_000)
    index = np.floor(x / 10).astype(np.int64)
    expected_ids, expected = aggregate_sorted(x, y, index)

    dense = BucketStats.empty(10)
    halves = (slice(1500, None), slice(0, 1500))  # 뒤쪽 절반을 먼저 넣는다
    for half in halves:
        ids, part = aggregate_sorted(x[half], y[half], index[half])
        merge_into(dense, ids, part)

    picked = dense.take(expected_ids)
    for name in ("count", "first_x", "first_y", "last_x", "last_y", "min_y", "max_y"):
        np.testing.assert_array_equal(getattr(picked, name), getattr(expected, name), name)


def test_all_missing_bucket_records_gap_only():
    ids, stats = aggregate_sorted(
        np.array([1.0, 2.0, 3.0]), np.array([np.nan, np.nan, 5.0]), np.array([0, 0, 1])
    )
    assert list(ids) == [0, 1]
    assert list(stats.count) == [0, 1]
    assert list(stats.has_gap) == [True, False]
    assert (stats.gap_first_x[0], stats.gap_last_x[0]) == (1.0, 2.0)
    assert stats.y_bounds() == (5.0, 5.0)


def test_empty_input():
    ids, stats = aggregate_sorted(np.empty(0), np.empty(0), np.empty(0, dtype=np.int64))
    assert len(ids) == 0 and len(stats) == 0
    assert BucketStats.empty(3).y_bounds() is None
