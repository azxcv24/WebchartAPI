"""Integration tests for POST /datasets/connect."""

import time


def test_connect_unsupported_protocol(client):
    resp = client.post(
        "/api/v1/datasets/connect",
        json={"protocol": "gopher", "uri": "gopher://h/x"},
    )
    # Unknown scheme -> ProtocolNotSupportedError (400)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "PROTOCOL_NOT_SUPPORTED"


def test_connect_s3_lifecycle(client, monkeypatch, tmp_data_dir):
    """connect returns INGESTING immediately; a stubbed S3 backend drives
    the dataset to READY."""
    import pandas as pd

    from webchart.services.ingest import s3 as s3_module

    sample = pd.DataFrame({"x": range(30), "y": [i * 2.0 for i in range(30)]})

    class FakeClient:
        def head_object(self, Bucket, Key):  # noqa: N803 (boto3 kwarg names)
            return {"ContentLength": 1024}

        def download_file(self, bucket, key, filename):
            sample.to_csv(filename, index=False)

    monkeypatch.setattr(s3_module.S3IngestBackend, "_client", staticmethod(lambda spec: FakeClient()))

    resp = client.post(
        "/api/v1/datasets/connect",
        json={
            "protocol": "s3",
            "uri": "s3://bucket/data/file.csv",
            "credentials": {"access_key": "AK", "secret_key": "SK"},
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "ingesting"
    dataset_id = body["dataset_id"]

    # poll to READY
    deadline = time.time() + 5
    final = None
    while time.time() < deadline:
        s = client.get(f"/api/v1/datasets/{dataset_id}")
        if s.status_code == 200 and s.json()["status"] == "ready":
            final = s.json()
            break
        time.sleep(0.05)
    assert final is not None, "dataset never reached READY"
    assert final["row_count"] == 30
    assert final["source_format"] == "csv"


def test_connect_s3_failure_marks_error(client, monkeypatch, tmp_data_dir):
    from webchart.core.errors import ConnectorError
    from webchart.services.ingest import s3 as s3_module

    async def boom(self, spec, dest):  # noqa: ANN001
        raise ConnectorError("S3 download failed: access denied")

    monkeypatch.setattr(s3_module.S3IngestBackend, "ingest_to_parquet", boom)

    resp = client.post(
        "/api/v1/datasets/connect",
        json={"protocol": "s3", "uri": "s3://bucket/data/file.csv"},
    )
    dataset_id = resp.json()["dataset_id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        s = client.get(f"/api/v1/datasets/{dataset_id}").json()
        if s["status"] == "error":
            assert "access denied" in (s.get("error_message") or "")
            return
        time.sleep(0.05)
    raise AssertionError("dataset never reached ERROR")
