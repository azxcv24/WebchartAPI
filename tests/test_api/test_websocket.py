"""WebSocket interaction tests for session lifecycle."""

import io
import time

import pandas as pd


def _ready_dataset(client) -> str:
    df = pd.DataFrame({"x": range(200), "y": [i * 1.5 for i in range(200)]})
    r = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", io.BytesIO(df.to_csv(index=False).encode()), "text/csv")},
    )
    dsid = r.json()["dataset_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.get(f"/api/v1/datasets/{dsid}").json()["status"] == "ready":
            return dsid
        time.sleep(0.05)
    raise AssertionError("dataset never ready")


def _create_session(client, dataset_id: str) -> str:
    body = {
        "dataset_id": dataset_id,
        "chart_config": {
            "dataset_id": dataset_id,
            "x_axis": {"column": "x"},
            "y_axis": {"column": "y"},
        },
        "viewport": {"x_range": [0, 200], "zoom": 1},
    }
    r = client.post("/api/v1/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def test_ws_ping_pong(client):
    dsid = _ready_dataset(client)
    sid = _create_session(client, dsid)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_ws_unknown_message_does_not_disconnect(client):
    dsid = _ready_dataset(client)
    sid = _create_session(client, dsid)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_json({"type": "nope"})
        err = ws.receive_json()
        assert err["code"] == "UNKNOWN_MESSAGE"
        # Connection still alive: ping should round-trip
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_ws_invalid_json_returns_error(client):
    dsid = _ready_dataset(client)
    sid = _create_session(client, dsid)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_text("{not json")
        err = ws.receive_json()
        assert err["code"] == "INVALID_JSON"


def test_ws_invalid_viewport_payload(client):
    dsid = _ready_dataset(client)
    sid = _create_session(client, dsid)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        # Missing required x_range field
        ws.send_json({"type": "viewport_change", "zoom": 1})
        err = ws.receive_json()
        assert err["code"] == "INVALID_MESSAGE"
