"""오라클 래스터 비교(webchart.testing.raster) 자체 검증."""

import numpy as np

from webchart.testing.raster import compare_envelopes


def _mask(columns: dict[int, tuple[int, int]], width: int = 6, height: int = 20) -> np.ndarray:
    mask = np.zeros((height, width), dtype=bool)
    for column, (top, bottom) in columns.items():
        mask[top : bottom + 1, column] = True
    return mask


def test_identical_masks_match():
    mask = _mask({1: (5, 9), 2: (8, 12)})
    diff = compare_envelopes(mask, mask)
    assert diff.mismatched_columns == 0 and diff.max_deviation_px == 0


def test_lost_peak_is_detected_even_with_reach():
    expected = _mask({1: (5, 9), 2: (0, 12), 3: (8, 10)})  # 열 2에 높은 피크
    actual = _mask({1: (5, 9), 2: (8, 12), 3: (8, 10)})
    diff = compare_envelopes(expected, actual, tolerance_px=1, reach_px=1)
    assert diff.mismatched_columns == 1
    assert diff.max_deviation_px == 5  # 피크 0행 vs 이웃 최고 5행


def test_spill_into_neighbor_column_is_within_reach():
    """이웃 열로 번진 잉크(열 1이 열 2의 높이까지 칠해짐)는 reach 1에서 허용된다."""
    expected = _mask({1: (2, 9), 2: (2, 12)})
    actual = _mask({1: (5, 9), 2: (2, 12)})
    assert compare_envelopes(expected, actual, reach_px=0).mismatched_columns == 1
    assert compare_envelopes(expected, actual, reach_px=1).mismatched_columns == 0


def test_missing_column_counts_full_height():
    expected = _mask({1: (5, 9)})
    actual = _mask({})
    diff = compare_envelopes(expected, actual)
    assert diff.mismatched_columns == 1 and diff.max_deviation_px == 20
