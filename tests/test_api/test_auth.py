"""Auth and rate-limit tests."""

from webchart.config import settings


def test_auth_required_when_keys_configured(client, require_auth):
    # GET /datasets without a key
    r = client.get("/api/v1/datasets")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "UNAUTHORIZED"


def test_auth_accepts_valid_key(client, require_auth):
    r = client.get("/api/v1/datasets", headers={"X-API-Key": require_auth})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_auth_rejects_wrong_key(client, require_auth):
    r = client.get("/api/v1/datasets", headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_health_bypasses_auth(client, require_auth):
    r = client.get("/health")
    assert r.status_code == 200


def test_session_owner_check_blocks_other_caller(client, require_auth, monkeypatch):
    # Stub a fully ready dataset on disk so /sessions can succeed
    from webchart.models.dataset import DatasetMeta, DatasetStatus

    ds_dir = settings.data_dir / "datasets" / "ds-owned"
    ds_dir.mkdir(parents=True)
    (ds_dir / "meta.json").write_text(
        DatasetMeta(
            dataset_id="ds-owned",
            name="ds-owned",
            status=DatasetStatus.READY,
        ).model_dump_json()
    )

    body = {
        "dataset_id": "ds-owned",
        "chart_config": {
            "dataset_id": "ds-owned",
            "x_axis": {"column": "x"},
            "y_axis": {"column": "y"},
        },
        "viewport": {"x_range": [0, 10]},
    }
    create = client.post(
        "/api/v1/sessions", json=body, headers={"X-API-Key": "test-key"}
    )
    assert create.status_code == 201
    sid = create.json()["session_id"]

    # Different valid key cannot read the session
    other = client.get(
        f"/api/v1/sessions/{sid}", headers={"X-API-Key": "other-key"}
    )
    assert other.status_code == 403


def test_rate_limit_returns_429(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)

    # /health is exempt; use /api/v1/datasets which goes through the limiter
    # (auth is off so just need a non-exempt path)
    for _ in range(3):
        assert client.get("/api/v1/datasets").status_code == 200
    r = client.get("/api/v1/datasets")
    assert r.status_code == 429
    assert "Retry-After" in r.headers
