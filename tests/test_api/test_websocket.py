"""WebSocket 프로토콜 v2 테스트."""

import io
import json
import struct
import time

import numpy as np
import pandas as pd

from webchart.models.ws_messages import TILE_HEADER_FORMAT, TILE_HEADER_MAGIC

HEADER_SIZE = struct.calcsize(TILE_HEADER_FORMAT)
ROWS = 5_000


def _ready_dataset(client) -> str:
    x = np.arange(ROWS, dtype=float)
    df = pd.DataFrame({"x": x, "y": np.sin(x / 50.0) * 10, "z": x * 2})
    r = client.post(
        "/api/v1/datasets/upload",
        files={"file": ("d.csv", io.BytesIO(df.to_csv(index=False).encode()), "text/csv")},
    )
    dsid = r.json()["dataset_id"]
    deadline = time.time() + 10
    while client.get(f"/api/v1/datasets/{dsid}").json()["status"] != "ready":
        assert time.time() < deadline, "dataset never ready"
        time.sleep(0.05)
    return dsid


def _create_session(client, dataset_id: str) -> str:
    body = {
        "dataset_id": dataset_id,
        "chart_config": {
            "dataset_id": dataset_id,
            "x_axis": {"column": "x"},
            "layers": [
                {"kind": "line", "column": "y"},
                {"kind": "line", "column": "z", "axis": "y2"},
            ],
        },
        "viewport": {"x_range": [0, ROWS], "zoom": 0},
    }
    r = client.post("/api/v1/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def _session(client):
    dsid = _ready_dataset(client)
    return _create_session(client, dsid)


def _receive_frame(ws, req_id: int):
    """frame_begin → 타일 바이너리들 → frame_end 를 모은다 (다른 req_id 메시지는 건너뛴다)."""
    begin = None
    tiles = []
    while True:
        message = ws.receive()
        if message.get("bytes") is not None:
            header = struct.unpack(TILE_HEADER_FORMAT, message["bytes"][:HEADER_SIZE])
            if header[2] == req_id:
                tiles.append((header, message["bytes"][HEADER_SIZE:]))
            continue
        payload = json.loads(message["text"])
        if payload.get("req_id") != req_id:
            continue
        if payload["type"] == "frame_begin":
            begin = payload
        elif payload["type"] == "frame_end":
            return begin, tiles, payload
        elif payload["type"] == "error":
            raise AssertionError(payload)


def test_ws_ping_pong(client):
    sid = _session(client)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_ws_unknown_message_does_not_disconnect(client):
    sid = _session(client)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_json({"type": "nope"})
        assert ws.receive_json()["code"] == "UNKNOWN_MESSAGE"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_ws_invalid_json_returns_error(client):
    sid = _session(client)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_text("{not json")
        assert ws.receive_json()["code"] == "INVALID_JSON"


def test_ws_invalid_viewport_payload(client):
    sid = _session(client)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_json({"type": "viewport", "req_id": 3})  # x_range 누락
        error = ws.receive_json()
        assert (error["code"], error["req_id"]) == ("INVALID_MESSAGE", 3)


def test_viewport_streams_a_frame(client):
    sid = _session(client)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_json(
            {
                "type": "viewport",
                "req_id": 7,
                "x_range": [0, ROWS],
                "zoom": 1,
                "width_px": 1024,
                "height_px": 512,
                "dpr": 2,
            }  # fmt: skip
        )
        begin, tiles, end = _receive_frame(ws, 7)

    assert begin["zoom"] == 1 and begin["tile_size_px"] == 1024
    assert set(begin["y_ranges"]) == {"y", "y2"}  # 축마다 공유 y범위
    assert begin["x_ticks"] and begin["y_ticks"]["y"]
    visible = [(t["zoom"], t["index"]) for t in begin["tiles"] if t["visible"]]
    assert visible == [(1, 0), (1, 1)]
    # 보이는 타일이 먼저 온다
    received = [(h[3], h[4]) for h, _ in tiles]
    assert set(received[: len(visible)]) == set(visible)
    for header, image in tiles:
        assert header[:2] == (TILE_HEADER_MAGIC, 2) and header[5] == 2
        assert image[:8] == b"\x89PNG\r\n\x1a\n"
    assert end["sent"] == len(tiles) and end["exact"] is True


def test_newer_viewport_wins(client):
    """연달아 보낸 viewport 중 마지막 것의 프레임은 반드시 끝까지 온다."""
    sid = _session(client)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        for req_id in range(1, 6):
            start = req_id * 100.0
            ws.send_json(
                {"type": "viewport", "req_id": req_id, "x_range": [start, start + 1000], "zoom": 2}
            )
        begin, tiles, end = _receive_frame(ws, 5)
        assert begin["x_range"] == [500.0, 1500.0]
        assert end["sent"] == len(tiles) > 0


def test_hover_returns_raw_values_for_every_series(client):
    sid = _session(client)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_json({"type": "viewport", "req_id": 1, "x_range": [0, 1000], "zoom": 2,
                      "width_px": 1000})  # fmt: skip
        _receive_frame(ws, 1)
        ws.send_json({"type": "hover", "req_id": 2, "pixel_x": 421.4})
        crosshair = ws.receive_json()

    assert crosshair["type"] == "crosshair" and crosshair["req_id"] == 2
    by_series = {p["series"]: p for p in crosshair["points"]}
    assert by_series["y"]["x"] == 421.0  # 화면 421.4px = x 421.4 → 가장 가까운 원본 행
    assert by_series["y"]["y"] == np.sin(421 / 50.0) * 10
    assert by_series["z"]["y"] == 842.0
    assert by_series["y"]["label"] == "421"


def test_select_returns_exact_range_stats(client):
    sid = _session(client)
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        ws.send_json({"type": "select", "req_id": 9, "x_range": [100, 200]})
        stats = ws.receive_json()

    assert stats["type"] == "range_stats" and stats["exact"] is True
    z = stats["series"]["z"]
    assert (z["count"], z["min"], z["max"], z["first"], z["last"]) == (100, 200, 398, 200, 398)
    assert z["mean"] == 299.0


def test_append_pushes_update_and_reuses_untouched_tiles(client):
    sid = _session(client)
    dsid = client.get(f"/api/v1/sessions/{sid}").json()["dataset_id"]
    with client.websocket_connect(f"/api/v1/ws/{sid}") as ws:
        viewport = {"type": "viewport", "x_range": [0, ROWS], "zoom": 3, "width_px": 4096}
        ws.send_json({**viewport, "req_id": 1})
        _, first_tiles, first_end = _receive_frame(ws, 1)
        assert first_end["cached"] == 0

        # 4,000~4,010 구간에 행 추가 → 그 구간 타일만 새로 그려야 한다
        x = [4_000.5 + i for i in range(10)]
        response = client.post(
            f"/api/v1/datasets/{dsid}/rows",
            json={"columns": {"x": x, "y": [0.0] * 10, "z": [9.0] * 10}},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rows"] == 10 and body["generation"] == 1

        pushed = ws.receive_json()
        assert pushed["type"] == "data_appended" and pushed["generation"] == 1
        low, high = pushed["x_range"]
        assert low <= 4_000.5 and high >= 4_009.5

        ws.send_json({**viewport, "req_id": 2})
        _, tiles, end = _receive_frame(ws, 2)
    assert end["sent"] == len(first_tiles)
    # y범위는 global 정책(통계)이고 추가값이 기존 범위 안이라, 닿지 않은 타일은 캐시에서 온다
    assert 0 < end["cached"] < end["sent"]
