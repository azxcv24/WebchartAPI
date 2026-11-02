"""축 눈금 서비스 테스트."""

import pandas as pd
import pytest

from webchart.domain.ticks import numeric_ticks, place, target_tick_count, time_ticks


def _ns(text: str) -> float:
    return float(pd.Timestamp(text).value)


class TestNumericTicks:
    def test_nice_steps_inside_range(self):
        ticks = numeric_ticks(-3.2, 97.1, target_count=5)
        assert [t.value for t in ticks] == [0, 20, 40, 60, 80]
        assert [t.label for t in ticks] == ["0", "20", "40", "60", "80"]

    def test_decimals_follow_step(self):
        ticks = numeric_ticks(0.0, 1.0, target_count=4)
        assert [t.label for t in ticks] == ["0.0", "0.2", "0.4", "0.6", "0.8", "1.0"]

    def test_no_negative_zero(self):
        assert numeric_ticks(-1.0, 1.0, target_count=2)[1].label == "0"

    def test_place_maps_range_to_pixels(self):
        placed = place(numeric_ticks(0, 100, 2), 0, 100, length_px=500)
        assert [t.pixel for t in placed] == [0, 250, 500]


class TestTimeTicks:
    def test_hours_align_to_local_midnight(self):
        start, end = _ns("2026-01-01T13:30Z"), _ns("2026-01-02T13:30Z")
        ticks = time_ticks(start, end, target_count=5, timezone="Asia/Seoul")
        # 6시간 간격, 한국 시각 00·06·12·18시
        assert [t.label for t in ticks] == [
            "01-02 00:00",
            "06:00",
            "12:00",
            "18:00",
        ]
        assert ticks[0].value == _ns("2026-01-01T15:00Z")  # 한국 자정 = UTC 15시

    def test_naive_time_uses_wall_clock(self):
        ticks = time_ticks(_ns("2026-03-01"), _ns("2026-03-01 00:00:10"), 5)
        assert [t.label for t in ticks][:3] == ["03-01 00:00:00", "00:00:02", "00:00:04"]

    def test_months_and_years(self):
        months = time_ticks(_ns("2026-01-15"), _ns("2026-12-15"), 4)
        assert [t.label for t in months] == ["2026-04", "2026-07", "2026-10"]
        years = time_ticks(_ns("2000-06-01"), _ns("2030-06-01"), 4)
        assert [t.label for t in years] == ["2010", "2020", "2030"]

    def test_milliseconds(self):
        ticks = time_ticks(_ns("2026-01-01"), _ns("2026-01-01 00:00:00.010"), 5)
        assert ticks[1].label == "00:00:00.002"

    @pytest.mark.parametrize("length, expected", [(50, 2), (1000, 10)])
    def test_target_count(self, length, expected):
        assert target_tick_count(length) == expected


def test_multi_day_ticks_stay_evenly_spaced_across_months():
    ticks = time_ticks(_ns("2025-12-20"), _ns("2026-01-25"), 6)
    gaps = {b.value - a.value for a, b in zip(ticks, ticks[1:], strict=False)}
    assert len(gaps) == 1  # 월이 바뀌어도 간격이 같다
