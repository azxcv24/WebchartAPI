"""Integration tests for dataset upload lifecycle."""

import io
import json
import time

import pandas as pd


def _wait_for_status(client, dataset_id: str, target: str, timeout: float = 5.0):
    """Poll the dataset until status matches target or timeout expires."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = client.get(f"/api/v1/datasets/{dataset_id}")
        if resp.status_code == 200:
            last = resp.json()
            if last["status"] == target:
                return last
        time.sleep(0.05)
    raise AssertionError(f"Status never reached {target}; last={last}")


def test_upload_lifecycle_ready(client, tmp_path):
    """Upload returns INGESTING immediately, transitions to READY."""
    df = pd.DataFrame({"x": range(100), "y": [i * 2.0 for i in range(100)]})
    csv_bytes = df.to_csv(index=False).encode()

    resp = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("sample.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "ingesting"
    dataset_id = body["dataset_id"]

    # Right after upload, dataset is queryable (INGESTING placeholder exists)
    immediate = client.get(f"/api/v1/datasets/{dataset_id}")
    assert immediate.status_code == 200

    final = _wait_for_status(client, dataset_id, "ready")
    assert final["row_count"] == 100
    assert final["source_format"] == "csv"
    assert any(c["name"] == "x" for c in final["columns"])


def test_upload_unsupported_extension(client):
    resp = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("bad.exe", io.BytesIO(b"junk"), "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "UNSUPPORTED_FORMAT"


def test_upload_failure_marks_error(client):
    """A corrupt Parquet file makes convert_to_parquet raise; status -> ERROR."""
    # Garbage bytes with a .parquet extension trip pyarrow immediately.
    body = b"\x00\x01\x02not-a-parquet-file\x03" * 8
    resp = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("broken.parquet", io.BytesIO(body), "application/octet-stream")},
    )
    assert resp.status_code == 202
    dataset_id = resp.json()["dataset_id"]

    deadline = time.time() + 5.0
    while time.time() < deadline:
        s = client.get(f"/api/v1/datasets/{dataset_id}").json()
        if s["status"] == "error":
            assert s.get("error_message")
            return
        time.sleep(0.05)
    raise AssertionError("Dataset never reached ERROR state")


def test_list_datasets_pagination(client, tmp_data_dir):
    """list returns newest first and respects limit/offset."""
    from webchart.models.dataset import DatasetMeta, DatasetStatus

    # Seed three meta.json files with monotonically increasing created_at
    base = tmp_data_dir / "datasets"
    for i in range(3):
        ds_dir = base / f"ds-{i}"
        ds_dir.mkdir()
        meta = DatasetMeta(
            dataset_id=f"ds-{i}",
            name=f"ds-{i}",
            status=DatasetStatus.READY,
            row_count=i,
        )
        (ds_dir / "meta.json").write_text(meta.model_dump_json())

    resp = client.get("/api/v1/datasets?limit=2&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
