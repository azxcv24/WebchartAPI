"""호버 해석 테스트: 픽셀 위치 → 최근접 원본 점."""

import pandas as pd

from webchart.models.chart import AxisConfig, ChartSpec
from webchart.models.session import SessionState, ViewportWindow
from webchart.services.ingest.upload import convert_to_parquet
from webchart.services.session.interaction import resolve_hover


def _ingest(tmp_data_dir, frame: pd.DataFrame, dataset_id: str) -> None:
    source = tmp_data_dir / "uploads" / f"{dataset_id}.parquet"
    frame.to_parquet(source, index=False)
    convert_to_parquet(source, dataset_id)


def _session(dataset_id: str, x_range: tuple[float, float]) -> SessionState:
    return SessionState(
        session_id="s",
        dataset_id=dataset_id,
        chart_config=ChartSpec(
            dataset_id=dataset_id, x_axis=AxisConfig(column="x"), y_columns=["y"]
        ),
        viewport=ViewportWindow(x_range=x_range, pixel_width=1000, pixel_height=500),
    )


async def test_hover_returns_nearest_raw_point(tmp_data_dir):
    _ingest(
        tmp_data_dir, pd.DataFrame({"x": range(100), "y": [i * 10.0 for i in range(100)]}), "num"
    )

    # 화면 폭 1000px = x 0..100 → 픽셀 421은 x 42.1, 가장 가까운 점은 x=42
    hover = await resolve_hover(_session("num", (0.0, 100.0)), pixel_x=421, pixel_y=0)

    assert hover is not None
    assert (hover.data_x, hover.data_y) == (42.0, 420.0)
    assert hover.label == "x=42"


async def test_hover_on_time_axis_uses_canonical_x(tmp_data_dir):
    times = pd.date_range("2026-01-01", periods=100, freq="s")
    _ingest(tmp_data_dir, pd.DataFrame({"x": times, "y": range(100)}), "time")
    start, end = times[0].value, times[-1].value

    hover = await resolve_hover(
        _session("time", (float(start), float(end))), pixel_x=500, pixel_y=0
    )

    assert hover is not None
    assert hover.data_y in (49.0, 50.0)  # 화면 가운데 = 49.5초
    assert hover.label.startswith("x=2026-01-01T00:00:")


async def test_hover_with_degenerate_viewport_returns_none(tmp_data_dir):
    _ingest(tmp_data_dir, pd.DataFrame({"x": range(10), "y": range(10)}), "flat")
    assert await resolve_hover(_session("flat", (5.0, 5.0)), pixel_x=1, pixel_y=1) is None
