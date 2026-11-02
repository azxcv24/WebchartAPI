"""정적 렌더 API: ChartSpec v2 레이어·이중 y축·시간축 눈금 (Phase 2)."""

import io

import numpy as np
import pandas as pd
import pytest
from PIL import Image

from webchart.application.static_chart import render_static_chart
from webchart.domain.geometry import XRange
from webchart.models.chart import ChartSpec
from webchart.services.ingest.upload import convert_to_parquet


@pytest.fixture
def market(tmp_data_dir) -> str:
    rows = 20_000
    rng = np.random.default_rng(0)
    times = pd.date_range("2026-01-01", periods=rows, freq="min", tz="Asia/Seoul")
    frame = pd.DataFrame(
        {
            "ts": times,
            "price": 100 + np.cumsum(rng.normal(0, 0.5, rows)),
            "volume": rng.integers(1, 1_000, rows).astype(float),
        }
    )
    source = tmp_data_dir / "uploads" / "market.parquet"
    frame.to_parquet(source, index=False)
    convert_to_parquet(source, "market")
    return "market"


def _spec(**layers_and_more) -> dict:
    return {"dataset_id": "market", "x_axis": {"column": "ts"}, "width": 800, "height": 400,
            **layers_and_more}  # fmt: skip


@pytest.mark.parametrize(
    "layer",
    [
        {"kind": "line", "column": "price"},
        {"kind": "area", "column": "price"},
        {"kind": "band", "column": "price"},
        {"kind": "bar", "column": "volume", "agg": "sum"},
        {"kind": "candlestick", "column": "price"},
        {"kind": "scatter", "column": "price"},
        {"kind": "heatmap", "column": "price", "normalization": "log"},
    ],
)
def test_every_layer_kind_renders_exactly(client, market, layer):
    response = client.post("/api/v1/charts/render", json=_spec(layers=[layer]))

    assert response.status_code == 200, response.text
    assert Image.open(io.BytesIO(response.content)).format == "PNG"
    assert response.headers["X-Webchart-Exact"] == "true"
    assert int(response.headers["X-Data-Points"]) > 0


def test_composite_chart_with_dual_axis(client, market):
    spec = _spec(
        layers=[
            {"kind": "candlestick", "column": "price", "group_px": 16},
            {"kind": "line", "column": "price", "style": {"color": "#e45", "label": "가격"}},
            {"kind": "bar", "column": "volume", "agg": "sum", "axis": "y2"},
        ],
        axes={"y": {"label": "가격"}, "y2": {"label": "거래량", "range_policy": "viewport"}},
    )
    response = client.post("/api/v1/charts/render", json=spec)
    assert response.status_code == 200, response.text
    assert response.headers["X-Webchart-Exact"] == "true"


def test_v1_spec_is_adapted_to_layers():
    spec = ChartSpec(
        dataset_id="d",
        chart_type="bar",
        x_axis={"column": "x"},
        y_axis={"column": "y", "range": (0, 10)},
    )
    assert [(layer.kind.value, layer.column) for layer in spec.layers] == [("bar", "y")]
    assert spec.axis("y").range == (0, 10)


def test_layer_validation():
    with pytest.raises(ValueError):
        ChartSpec(dataset_id="d", x_axis={"column": "x"}, layers=[{"kind": "line"}])
    with pytest.raises(ValueError):
        ChartSpec(
            dataset_id="d",
            x_axis={"column": "x"},
            layers=[{"kind": "bar", "column": "y", "group_px": 6}],
        )


def test_time_axis_ticks_use_display_timezone(market):
    """시간축 눈금은 컬럼의 원래 시간대(Asia/Seoul)의 벽시계 값이다 (UTC 아님)."""
    from webchart.application.axes import x_ticks
    from webchart.services.data.access import get_dataset_meta

    meta = get_dataset_meta(market)
    ts = next(c for c in meta.columns if c.name == "ts")
    spec = ChartSpec(**_spec(layers=[{"kind": "line", "column": "price"}]))
    start = pd.Timestamp("2026-01-01 00:00", tz="Asia/Seoul").value
    view = XRange(float(start), float(start + 6 * 3_600 * 10**9))

    ticks = x_ticks(spec, ts, view, 600)
    assert ticks[0].label.endswith("00:00") and ticks[0].value == start

    in_utc = spec.model_copy(update={"x_axis": spec.x_axis.model_copy(update={"timezone": "UTC"})})
    assert x_ticks(in_utc, ts, view, 600)[0].label == "12-31 15:00"


def test_static_chart_reports_points_for_each_layer(market):
    spec = ChartSpec(
        **_spec(layers=[{"kind": "line", "column": "price"}, {"kind": "bar", "column": "volume"}])
    )
    rendered = render_static_chart(spec)
    assert rendered.is_exact and rendered.source == "raw"
    assert rendered.point_count > 0
