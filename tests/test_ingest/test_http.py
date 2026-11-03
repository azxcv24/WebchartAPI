"""End-to-end test for the HTTP connector against a local server."""

import functools
import http.server
import threading

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

    dest = tmp_data_dir / "datasets" / "ds-http"
    meta = await backend.ingest_to_parquet(spec, dest)
    assert meta.row_count == 80
    assert meta.source_format == "csv"
    assert {c.name for c in meta.columns} == {"x", "y"}


async def test_http_ssrf_guard_blocks_localhost(http_server, tmp_data_dir, monkeypatch):
    from webchart.config import settings
    from webchart.core.errors import ConnectorError

    monkeypatch.setattr(settings, "block_private_egress", True)
    base_url, serve_dir = http_server
    (serve_dir / "data.csv").write_text("x,y\n1,2\n")

    backend = HttpIngestBackend()
    spec = RemoteSourceSpec(protocol="http", uri=f"{base_url}/data.csv")
    # 사설·루프백 주소로 가는 다운로드는 거부한다
    with pytest.raises(ConnectorError):
        await backend.ingest_to_parquet(spec, tmp_data_dir / "datasets" / "ds-ssrf")


async def test_http_404_raises_connector_error(http_server, tmp_data_dir):
    from webchart.core.errors import ConnectorError

    base_url, _ = http_server
    backend = HttpIngestBackend()
    spec = RemoteSourceSpec(protocol="http", uri=f"{base_url}/missing.csv")

    with pytest.raises(ConnectorError):
        await backend.ingest_to_parquet(spec, tmp_data_dir / "datasets" / "ds-404")


async def test_credentials_are_not_forwarded_to_another_host(tmp_path, tmp_data_dir):
    """다른 호스트로 리다이렉트되면 Authorization 헤더를 보내지 않는다."""
    seen: dict[str, str | None] = {}

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 규약
            seen["authorization"] = self.headers.get("Authorization")
            body = b"x,y\n1,2\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    target_url = f"http://127.0.0.1:{target.server_address[1]}/data.csv"

    class Redirect(Target):
        def do_GET(self):  # noqa: N802
            seen["first"] = self.headers.get("Authorization")
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

    origin = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    for server in (target, origin):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        spec = RemoteSourceSpec(
            protocol="http",
            uri=f"http://127.0.0.1:{origin.server_address[1]}/data.csv",
            credentials={"bearer": "secret-token"},
        )
        meta = await HttpIngestBackend().ingest_to_parquet(spec, tmp_data_dir / "datasets" / "r")
    finally:
        target.shutdown()
        origin.shutdown()

    assert meta.row_count == 1
    assert seen["first"] == "Bearer secret-token"
    assert seen["authorization"] is None
