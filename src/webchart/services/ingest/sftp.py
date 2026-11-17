"""SFTP 인제스트 백엔드 (asyncssh).

URI:     sftp://host[:port]/path/file.csv
creds:   username, password 그리고/또는 private_key(PEM 텍스트), passphrase
options: known_hosts — 호스트 키 검증은 기본으로 켜져 있다. known_hosts 파일 경로를
         주거나, "insecure"로 명시적으로 끌 수 있다

크기 상한은 받기 전에 stat 크기로 한 번, 받는 동안 실제로 받은 양으로 한 번 더 지킨다.
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


class SftpIngestBackend:
    """SFTP로 파일을 내려받아 Parquet으로 변환한다."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["sftp"]

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        asyncssh = require_lib("asyncssh", "connectors")

        dataset_id = dest.name
        suffix = validate_remote_format(spec.uri)
        temp_path = temp_download_path(dataset_id, suffix)

        parsed = urlparse(spec.uri)
        host = parsed.hostname
        port = parsed.port or 22
        remote_path = unquote(parsed.path)
        address = guard_egress_host(host)
        if not host or not remote_path:
            raise ConnectorError(f"Invalid SFTP URI: {spec.uri}")

        creds = spec.credentials
        # 검사한 주소로 접속하되, known_hosts는 원래 호스트 이름으로 찾는다
        connect_kwargs: dict = {
            "host": address,
            "port": port,
            "host_key_alias": host,
            "connect_timeout": settings.connector_timeout,
        }
        if creds.get("username"):
            connect_kwargs["username"] = creds["username"]
        if creds.get("password"):
            connect_kwargs["password"] = creds["password"]
        if creds.get("private_key"):
            try:
                key = asyncssh.import_private_key(
                    creds["private_key"], passphrase=creds.get("passphrase")
                )
            except Exception as e:
                raise ConnectorError(f"Invalid SFTP private key: {e}") from e
            connect_kwargs["client_keys"] = [key]

        # 호스트 키 검증은 기본으로 켠다 (MITM 방지, 시스템 known_hosts 사용).
        # options.known_hosts:
        #   - 경로 문자열 -> 그 known_hosts 파일로 검증
        #   - "insecure"  -> 명시적 해제 (검증하지 않음)
        #   - 없음        -> asyncssh 기본 검증
        kh = spec.options.get("known_hosts")
        if kh == "insecure":
            connect_kwargs["known_hosts"] = None
        elif kh:
            connect_kwargs["known_hosts"] = kh

        try:
            async with asyncssh.connect(**connect_kwargs) as conn:
                async with conn.start_sftp_client() as sftp:
                    declared = (await sftp.stat(remote_path)).size
                    if declared is not None:
                        enforce_size(declared, suffix)
                    with CappedDownload(temp_path, suffix) as out:
                        async with sftp.open(remote_path, "rb") as remote:
                            while block := await remote.read(DOWNLOAD_CHUNK_BYTES):
                                out.write(block)
        except WebchartError:
            raise
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            raise ConnectorError(f"SFTP download failed for {spec.uri}: {e}") from e

        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
