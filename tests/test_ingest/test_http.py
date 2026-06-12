"""End-to-end test for the HTTP connector against a local server."""

import functools
import http.server
import threading
from pathlib import Path

import pandas as pd
import pytest

from webchart.models.dataset import RemoteSourceSpec
from webchart.services.ingest.http import HttpIngestBackend


@pytest.fixture
def http_server(tmp_path):
    """Serve tmp_path over HTTP on an ephemeral port."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)
    )
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", tmp_path
    httpd.shutdown()


async def test_http_download_and_convert(http_server, tmp_data_dir):
    base_url, serve_dir = http_server
    df = pd.DataFrame({"x": range(80), "y": [i * 1.5 for i in range(80)]})
    (serve_dir / "data.csv").write_text(df.to_csv(index=False))

    backend = HttpIngestBackend()
    spec = RemoteSourceSpec(protocol="http", uri=f"{base_url}/data.csv")

    assert await backend.validate_source(spec) is True

    dest = tmp_data_dir / "datasets" / "ds-http"
    meta = await backend.ingest_to_parquet(spec, dest)
    assert meta.row_count == 80
    assert meta.source_format == "csv"
    assert {c.name for c in meta.columns} == {"x", "y"}


async def test_http_404_raises_connector_error(http_server, tmp_data_dir):
    from webchart.core.errors import ConnectorError

    base_url, _ = http_server
    backend = HttpIngestBackend()
    spec = RemoteSourceSpec(protocol="http", uri=f"{base_url}/missing.csv")

    assert await backend.validate_source(spec) is False
    with pytest.raises(ConnectorError):
        await backend.ingest_to_parquet(spec, tmp_data_dir / "datasets" / "ds-404")
