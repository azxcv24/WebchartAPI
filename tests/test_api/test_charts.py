"""Integration tests for /charts/render."""

import io
import time

import pandas as pd


def _upload_ready_dataset(client) -> str:
    df = pd.DataFrame({"x": range(500), "y": [i * 1.5 for i in range(500)]})
    csv = df.to_csv(index=False).encode()
    r = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", io.BytesIO(csv), "text/csv")},
    )
    dataset_id = r.json()["dataset_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.get(f"/api/v1/datasets/{dataset_id}").json()["status"] == "ready":
            return dataset_id
        time.sleep(0.05)
    raise AssertionError("dataset never became ready")


def test_render_png(client):
    dataset_id = _upload_ready_dataset(client)
    spec = {
        "dataset_id": dataset_id,
        "chart_type": "line",
        "x_axis": {"column": "x"},
        "y_axis": {"column": "y"},
        "width": 400,
        "height": 200,
        "format": "png",
    }
    resp = client.post("/api/v1/charts/render", json=spec)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert int(resp.headers["X-Data-Points"]) > 0


def test_render_info_returns_metadata(client):
    dataset_id = _upload_ready_dataset(client)
    spec = {
        "dataset_id": dataset_id,
        "x_axis": {"column": "x"},
        "y_axis": {"column": "y"},
    }
    resp = client.post("/api/v1/charts/render/info", json=spec)
    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == "png"
    assert body["size_bytes"] > 0


def test_render_rejects_oversize_width(client):
    """ChartSpec.width is bounded — width=99999 should 422."""
    resp = client.post(
        "/api/v1/charts/render",
        json={
            "dataset_id": "any",
            "x_axis": {"column": "x"},
            "y_axis": {"column": "y"},
            "width": 99999,
        },
    )
    assert resp.status_code == 422


def test_render_time_axis_with_range_and_downsampling(client):
    """시간 x축 정적 렌더: 정규 좌표(epoch ns) 범위 + LTTB 다운샘플 경로 (D3 회귀 방지)."""
    times = pd.date_range("2026-01-01", periods=10_000, freq="s")
    df = pd.DataFrame({"ts": times.astype(str), "y": range(10_000)})
    r = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("t.csv", io.BytesIO(df.to_csv(index=False).encode()), "text/csv")},
    )
    dataset_id = r.json()["dataset_id"]
    deadline = time.time() + 5
    while client.get(f"/api/v1/datasets/{dataset_id}").json()["status"] != "ready":
        assert time.time() < deadline, "dataset never became ready"
        time.sleep(0.05)

    first_half = (float(times[0].value), float(times[4_999].value))
    spec = {
        "dataset_id": dataset_id,
        "x_axis": {"column": "ts", "range": first_half},
        "y_columns": ["y"],
        "width": 400,
        "height": 200,
    }
    r = client.post("/api/v1/charts/render", json=spec)

    assert r.status_code == 200, r.text
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert int(r.headers["X-Data-Points"]) == 800  # 5000행 → 2 × 400px
