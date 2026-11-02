"""YRange 테스트: matplotlib 자동 스케일과 같은 범위가 나와야 오라클 정답과 y축이 맞는다."""

import numpy as np
import pytest

from webchart.domain.y_range import YRange, nice_step


def _matplotlib_autoscale(values: list[float]) -> tuple[float, float]:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    axes = Figure().add_subplot()
    axes.plot(np.arange(len(values)), values)
    return axes.get_ylim()


@pytest.mark.parametrize(
    "values",
    [
        [0.0, 1.0],
        [-3.5, 120.25],
        [1e-12, 3e-12],
        [5.0, 5.0],  # 값이 하나 → 넓히기
        [0.0, 0.0],
        [-7.0, -7.0],
        [1e9, 1e9 + 1],
    ],
)
def test_autoscaled_matches_matplotlib(values):
    expected = _matplotlib_autoscale(values)
    actual = YRange.autoscaled(min(values), max(values)).as_tuple()
    assert actual == pytest.approx(expected, rel=1e-12, abs=1e-300)


def test_rejects_empty_or_infinite():
    with pytest.raises(ValueError):
        YRange(1.0, 1.0)
    with pytest.raises(ValueError):
        YRange(0.0, float("inf"))


def test_snapped_outward_contains_original_and_is_stable():
    original = YRange(-3.2, 97.1)
    snapped = original.snapped_outward()
    assert snapped.low <= original.low and snapped.high >= original.high
    # 조금 움직인 범위도 같은 값으로 스냅된다 (캐시 재사용)
    assert YRange(-3.0, 96.0).snapped_outward() == snapped


@pytest.mark.parametrize(
    "raw, expected", [(0.7, 1.0), (1.0, 1.0), (1.3, 2.0), (3.0, 5.0), (7.0, 10.0), (0.012, 0.02)]
)
def test_nice_step(raw, expected):
    assert nice_step(raw) == pytest.approx(expected)
