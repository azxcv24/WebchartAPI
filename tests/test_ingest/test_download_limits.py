"""원격 다운로드의 크기 상한과 접속 주소 고정 테스트.

- 크기 상한: 서버가 크기를 속여도 받은 양이 상한을 넘는 순간 끊고 쓰다 만 파일을 지운다.
- 주소 고정: SSRF 가드가 검사한 IP로만 접속한다 (DNS 리바인딩 방지).

FTP·SFTP·S3 드라이버는 선택 의존성이라 가짜 클라이언트로 동작만 확인한다.
"""

import http.server
import io
import socket
import threading
from types import SimpleNamespace

import pytest

from webchart.config import settings
from webchart.core.errors import ConnectorError, FileTooLargeError
from webchart.models.dataset import RemoteSourceSpec
from webchart.services.ingest import common, ftp, s3, sftp
from webchart.services.ingest import http as http_connector
from webchart.services.ingest.common import CappedDownload, guard_egress_host

CSV_BODY = b"x,y\n" + b"".join(f"{i},{i * 2}\n".encode() for i in range(200))
PUBLIC_IP = "93.184.216.34"  # 예시용 공인 주소 (실제로 접속하지 않는다)


@pytest.fixture
def small_limit(monkeypatch):
    """상한을 100바이트로 줄인다. CSV_BODY는 이보다 크다."""
    monkeypatch.setattr(settings, "max_upload_size", 100)


@pytest.fixture
def fake_dns(monkeypatch):
    """이름 → 주소 목록을 정해 두고, 이름별 조회 횟수를 센다. 가드는 켠다."""
    table: dict[str, list[str]] = {}
    lookups: dict[str, int] = {}
    real = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host in table:
            lookups[host] = lookups.get(host, 0) + 1
            entry = (socket.AF_INET, socket.SOCK_STREAM, 6, "")
            return [(*entry, (ip, port or 0)) for ip in table[host]]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(settings, "block_private_egress", True)
    return SimpleNamespace(table=table, lookups=lookups)


# --- 공통 도구 ---


def test_capped_download_stops_at_limit_and_removes_partial_file(tmp_path, small_limit):
    target = tmp_path / "out.csv"
    with pytest.raises(FileTooLargeError):
        with CappedDownload(target, ".csv") as out:
            for _ in range(10):
                out.write(b"x" * 30)
    assert not target.exists()


def test_capped_download_keeps_file_within_limit(tmp_path, small_limit):
    target = tmp_path / "out.csv"
    with CappedDownload(target, ".csv") as out:
        out.copy_from(io.BytesIO(b"a" * 100), chunk_bytes=7)
    assert target.read_bytes() == b"a" * 100


def test_guard_returns_checked_address(fake_dns):
    fake_dns.table["data.example"] = [PUBLIC_IP]
    assert guard_egress_host("data.example") == PUBLIC_IP


def test_guard_rejects_when_any_address_is_private(fake_dns):
    fake_dns.table["mixed.example"] = [PUBLIC_IP, "10.0.0.5"]
    with pytest.raises(ConnectorError):
        guard_egress_host("mixed.example")


def test_guard_off_returns_host_unchanged():
    # conftest가 가드를 꺼 둔다
    assert guard_egress_host("anything.example") == "anything.example"


# --- HTTP ---


@pytest.fixture
def csv_server():
    """CSV_BODY를 길이 헤더 없이(연결 종료로 끝을 알림) 돌려주고 받은 Host 헤더를 기록한다."""
    seen_hosts: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 규약
            seen_hosts.append(self.headers.get("Host", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(CSV_BODY)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], seen_hosts
    server.shutdown()


async def test_http_connects_to_checked_address_only(csv_server, fake_dns, monkeypatch):
    """이름은 가드에서 한 번만 풀고, 접속은 그 IP로 한다. Host 헤더는 원래 이름이다."""
    port, seen_hosts = csv_server
    fake_dns.table["data.example"] = ["127.0.0.1"]
    monkeypatch.setattr(common, "_is_blocked_ip", lambda ip: False)  # 테스트 서버가 루프백이라

    spec = RemoteSourceSpec(protocol="http", uri=f"http://data.example:{port}/data.csv")
    meta = await http_connector.HttpIngestBackend().ingest_to_parquet(
        spec, settings.data_dir / "datasets" / "pinned"
    )

    assert meta.row_count == 200
    assert fake_dns.lookups["data.example"] == 1  # httpx가 다시 풀지 않았다
    assert seen_hosts == [f"data.example:{port}"]


async def test_http_cuts_off_body_without_length_at_limit(csv_server, small_limit):
    port, _ = csv_server
    spec = RemoteSourceSpec(protocol="http", uri=f"http://127.0.0.1:{port}/data.csv")
    with pytest.raises(FileTooLargeError):
        await http_connector.HttpIngestBackend().ingest_to_parquet(
            spec, settings.data_dir / "datasets" / "big"
        )
    assert not (settings.data_dir / "uploads" / "big.csv").exists()


# --- FTP ---


class _FakeFtpStream:
    def __init__(self, body: bytes, log: dict):
        self.body, self.log = body, log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def iter_by_block(self, count: int):
        for start in range(0, len(self.body), 32):
            self.log["blocks_sent"] += 1
            yield self.body[start : start + 32]


def _fake_aioftp(body: bytes, declared_size: int | None):
    log = {"connect": None, "blocks_sent": 0, "closed": False, "client_kwargs": None}

    class Client:
        def __init__(self, **kwargs):
            log["client_kwargs"] = kwargs

        async def connect(self, host, port):
            log["connect"] = (host, port)

        async def login(self, user, password):
            pass

        async def stat(self, path):
            if declared_size is None:
                raise RuntimeError("MLST not supported")
            return {"size": str(declared_size)}

        def download_stream(self, path):
            return _FakeFtpStream(body, log)

        def close(self):
            log["closed"] = True

    return SimpleNamespace(Client=Client), log


async def _ftp_ingest(monkeypatch, body, declared_size, uri="ftp://files.example/data.csv"):
    fake, log = _fake_aioftp(body, declared_size)
    monkeypatch.setattr(ftp, "require_lib", lambda name, extra: fake)
    spec = RemoteSourceSpec(protocol="ftp", uri=uri)
    try:
        return await ftp.FtpIngestBackend().ingest_to_parquet(
            spec, settings.data_dir / "datasets" / "ftp-ds"
        ), log
    except Exception as e:
        e.log = log
        raise


async def test_ftp_downloads_and_converts(monkeypatch):
    meta, log = await _ftp_ingest(monkeypatch, CSV_BODY, len(CSV_BODY))
    assert meta.row_count == 200
    assert log["closed"]
    assert log["client_kwargs"]["trust_server_pasv_ipv4_address"] is False


async def test_ftp_rejects_declared_size_before_download(monkeypatch, small_limit):
    with pytest.raises(FileTooLargeError) as exc:
        await _ftp_ingest(monkeypatch, CSV_BODY, len(CSV_BODY))
    assert exc.value.log["blocks_sent"] == 0


@pytest.mark.parametrize("declared_size", [10, None], ids=["lying-size", "unknown-size"])
async def test_ftp_cuts_off_stream_at_limit(monkeypatch, small_limit, declared_size):
    with pytest.raises(FileTooLargeError) as exc:
        await _ftp_ingest(monkeypatch, CSV_BODY, declared_size)
    log = exc.value.log
    assert log["blocks_sent"] == 4  # 32바이트씩, 100바이트를 넘긴 4번째 조각에서 멈춘다
    assert log["closed"]
    assert not (settings.data_dir / "uploads" / "ftp-ds.csv").exists()


async def test_ftp_connects_to_checked_address(monkeypatch, fake_dns):
    fake_dns.table["files.example"] = [PUBLIC_IP]
    _, log = await _ftp_ingest(monkeypatch, CSV_BODY, len(CSV_BODY))
    assert log["connect"] == (PUBLIC_IP, 21)


# --- SFTP ---


def _fake_asyncssh(body: bytes, declared_size: int | None):
    log = {"connect_kwargs": None, "reads": 0}

    class RemoteFile:
        def __init__(self):
            self.offset = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def read(self, size):
            log["reads"] += 1
            block = body[self.offset : self.offset + 32]
            self.offset += len(block)
            return block

    class Sftp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def stat(self, path):
            return SimpleNamespace(size=declared_size)

        def open(self, path, mode):
            return RemoteFile()

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def start_sftp_client(self):
            return Sftp()

    def connect(**kwargs):
        log["connect_kwargs"] = kwargs
        return Connection()

    return SimpleNamespace(connect=connect), log


async def _sftp_ingest(monkeypatch, body, declared_size):
    fake, log = _fake_asyncssh(body, declared_size)
    monkeypatch.setattr(sftp, "require_lib", lambda name, extra: fake)
    spec = RemoteSourceSpec(protocol="sftp", uri="sftp://files.example/data.csv")
    try:
        return await sftp.SftpIngestBackend().ingest_to_parquet(
            spec, settings.data_dir / "datasets" / "sftp-ds"
        ), log
    except Exception as e:
        e.log = log
        raise


async def test_sftp_downloads_and_converts(monkeypatch):
    meta, log = await _sftp_ingest(monkeypatch, CSV_BODY, len(CSV_BODY))
    assert meta.row_count == 200


async def test_sftp_rejects_declared_size_before_download(monkeypatch, small_limit):
    with pytest.raises(FileTooLargeError) as exc:
        await _sftp_ingest(monkeypatch, CSV_BODY, len(CSV_BODY))
    assert exc.value.log["reads"] == 0


async def test_sftp_cuts_off_stream_at_limit(monkeypatch, small_limit):
    with pytest.raises(FileTooLargeError) as exc:
        await _sftp_ingest(monkeypatch, CSV_BODY, None)
    assert exc.value.log["reads"] == 4
    assert not (settings.data_dir / "uploads" / "sftp-ds.csv").exists()


async def test_sftp_connects_to_checked_address_with_host_key_alias(monkeypatch, fake_dns):
    fake_dns.table["files.example"] = [PUBLIC_IP]
    _, log = await _sftp_ingest(monkeypatch, CSV_BODY, len(CSV_BODY))
    assert log["connect_kwargs"]["host"] == PUBLIC_IP
    assert log["connect_kwargs"]["host_key_alias"] == "files.example"


# --- S3 ---


async def test_s3_cuts_off_body_larger_than_content_length(monkeypatch, small_limit):
    class Client:
        def get_object(self, Bucket, Key):  # noqa: N803 - boto3 규약
            return {"ContentLength": 10, "Body": io.BytesIO(CSV_BODY)}

    monkeypatch.setattr(s3.S3IngestBackend, "_client", staticmethod(lambda spec: Client()))
    spec = RemoteSourceSpec(protocol="s3", uri="s3://bucket/data.csv")
    with pytest.raises(FileTooLargeError):
        await s3.S3IngestBackend().ingest_to_parquet(spec, settings.data_dir / "datasets" / "s3")
    assert not (settings.data_dir / "uploads" / "s3.csv").exists()
