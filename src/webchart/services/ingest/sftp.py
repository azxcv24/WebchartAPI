"""SFTP 인제스트 백엔드 (asyncssh).

URI:     sftp://host[:port]/path/file.csv
creds:   username, password 그리고/또는 private_key(PEM 텍스트), passphrase
options: known_hosts — 호스트 키 검증은 기본으로 켜져 있다. known_hosts 파일 경로를
         주거나, "insecure"로 명시적으로 끌 수 있다
"""

import logging
from pathlib import Path
from urllib.parse import unquote, urlparse

from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import (
    enforce_size,
    require_lib,
    temp_download_path,
    validate_remote_format,
)
from webchart.services.ingest.upload import convert_to_parquet

logger = logging.getLogger("webchart.ingest.sftp")


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
        if not host or not remote_path:
            raise ConnectorError(f"Invalid SFTP URI: {spec.uri}")

        creds = spec.credentials
        connect_kwargs: dict = {"host": host, "port": port}
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
                    await sftp.get(remote_path, str(temp_path))
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            raise ConnectorError(f"SFTP download failed for {spec.uri}: {e}") from e

        # sftp.get은 전송 도중 끊을 수 없어, 다 받은 뒤 크기 상한을 검사한다.
        try:
            enforce_size(temp_path.stat().st_size, suffix, spec.uri)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
