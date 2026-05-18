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
