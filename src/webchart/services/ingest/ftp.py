"""FTP 인제스트 백엔드 (aioftp).

URI:     ftp://host[:port]/path/file.csv
creds:   username, password (없으면 anonymous)

크기 상한은 두 번 지킨다. 받기 전에 서버가 알려 준 크기로 거르고, 받는 동안 실제로 받은
양을 세어 상한을 넘는 순간 끊는다(서버가 크기를 속여도 디스크를 채우지 못한다).
"""

from pathlib import Path
from urllib.parse import unquote, urlparse

from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError, WebchartError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import (
    DOWNLOAD_CHUNK_BYTES,
    CappedDownload,
    enforce_size,
    guard_egress_host,
    require_lib,
    temp_download_path,
    validate_remote_format,
)
from webchart.services.ingest.upload import convert_to_parquet


async def _declared_size_bytes(client, remote_path: str) -> int | None:
    """서버가 알려 주는 파일 크기. MLST·LIST로 알 수 없으면 None (받으면서 센다)."""
    try:
        info = await client.stat(remote_path)
        return int(info["size"])
    except Exception:
        return None


class FtpIngestBackend:
    """FTP로 파일을 내려받아 Parquet으로 변환한다."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["ftp"]

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        aioftp = require_lib("aioftp", "connectors")

        dataset_id = dest.name
        suffix = validate_remote_format(spec.uri)
        temp_path = temp_download_path(dataset_id, suffix)

        parsed = urlparse(spec.uri)
        host = parsed.hostname
        port = parsed.port or 21
        user = spec.credentials.get("username", "anonymous")
        password = spec.credentials.get("password", "")
        remote_path = unquote(parsed.path)

        address = guard_egress_host(host)
        if not host or not remote_path:
            raise ConnectorError(f"Invalid FTP URI: {spec.uri}")

        # 데이터 연결은 PASV 응답에 적힌 IP가 아니라 접속한 서버 주소로 연다.
        # 악성 서버가 PASV로 내부망 주소를 알려 주어 그곳을 읽게 하는 것을 막는다.
        client = aioftp.Client(
            socket_timeout=settings.connector_timeout,
            connection_timeout=settings.connector_timeout,
            trust_server_pasv_ipv4_address=False,
        )
        try:
            await client.connect(address, port)
            await client.login(user, password)
            declared = await _declared_size_bytes(client, remote_path)
            if declared is not None:
                enforce_size(declared, suffix)
            with CappedDownload(temp_path, suffix) as out:
                async with client.download_stream(remote_path) as stream:
                    async for block in stream.iter_by_block(DOWNLOAD_CHUNK_BYTES):
                        out.write(block)
        except WebchartError:
            raise
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            raise ConnectorError(f"FTP download failed for {spec.uri}: {e}") from e
        finally:
            # QUIT은 보내지 않는다. 전송을 도중에 끊은 뒤에는 서버가 426을 먼저 보내
            # QUIT 응답 검사가 원래 오류를 덮어쓰기 때문이다. 소켓만 닫는다.
            client.close()

        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
