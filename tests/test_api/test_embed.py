"""차트 등록·임베드 이미지·XYZ 타일 테스트 (계획 문서 3.4)."""

import io

import numpy as np
import pandas as pd
import pytest
from PIL import Image

from webchart.config import settings
from webchart.services.ingest.upload import convert_to_parquet

SPEC = {
    "dataset_id": "embed",
    "x_axis": {"column": "x"},
    "layers": [
        {"kind": "line", "column": "y"},
        {"kind": "bar", "column": "y", "agg": "count", "axis": "y2"},
    ],
    "width": 600,
    "height": 300,
}


@pytest.fixture
def dataset(tmp_data_dir):
    x = np.arange(20_000, dtype=float)
    frame = pd.DataFrame({"x": x, "y": np.sin(x / 300)})
    source = tmp_data_dir / "uploads" / "embed.parquet"
    frame.to_parquet(source, index=False)
    convert_to_parquet(source, "embed")


def test_same_spec_same_chart_id(client, dataset):
    first = client.post("/api/v1/charts", json=SPEC).json()
    second = client.post("/api/v1/charts", json=SPEC).json()
    assert first["chart_id"] == second["chart_id"]
    assert client.get(f"/api/v1/charts/{first['chart_id']}").json()["dataset_id"] == "embed"


def test_image_url_with_etag_revalidation(client, dataset):
    registered = client.post("/api/v1/charts", json=SPEC).json()
    url = registered["image_url"]

    response = client.get(url)
    assert response.status_code == 200
    assert Image.open(io.BytesIO(response.content)).size[0] > 0
    assert response.headers["X-Webchart-Exact"] == "true"
    etag = response.headers["ETag"]

    again = client.get(url, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""

    zoomed = client.get(f"/api/v1/charts/{registered['chart_id']}/image.png?x0=100&x1=200&w=300")
    assert zoomed.status_code == 200 and zoomed.headers["ETag"] != etag


def test_xyz_tiles(client, dataset):
    registered = client.post("/api/v1/charts", json=SPEC).json()
    template = registered["tile_url_template"]

    url = template.replace("{z}", "2").replace("{i}", "3").replace("{dpr}", "2")
    tile = client.get(url)
    assert tile.status_code == 200
    assert Image.open(io.BytesIO(tile.content)).size == (2 * settings.tile_size,) * 2
    assert tile.headers["X-Webchart-Exact"] == "true"
    assert client.get(url, headers={"If-None-Match": tile.headers["ETag"]}).status_code == 304

    out_of_range = template.replace("{z}", "1").replace("{i}", "2").replace("{dpr}", "1")
    assert client.get(out_of_range).status_code == 404


def test_embed_requires_token_when_auth_is_on(client, dataset, require_auth):
    registered = client.post("/api/v1/charts", json=SPEC, headers={"X-API-Key": require_auth})
    body = registered.json()
    assert body["token"] and "token=" in body["image_url"]

    assert client.get(body["image_url"]).status_code == 200  # 토큰으로 열린다
    bare = body["image_url"].split("?")[0]
    assert client.get(bare).status_code == 401
    assert client.get(bare + "?token=" + "0" * 32).status_code == 401
    assert client.get(bare, headers={"X-API-Key": require_auth}).status_code == 200


def test_unknown_chart_is_404(client, dataset):
    assert client.get("/api/v1/charts/" + "a" * 20 + "/image.png").status_code == 404
    assert client.get("/api/v1/charts/../../etc/image.png").status_code == 404


def test_viewer_is_served(client):
    page = client.get("/viewer/")
    assert page.status_code == 200 and 'import { WebChart } from "./webchart.js"' in page.text
    script = client.get("/viewer/webchart.js")
    assert script.status_code == 200 and "export class WebChart" in script.text
    assert len(script.content) < 15 * 1024  # 계획 문서 5장: 15KB 이하
