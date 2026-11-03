"""FTP 인제스트 백엔드 (aioftp).

URI:     ftp://host[:port]/path/file.csv
creds:   username, password (없으면 anonymous)
"""

from pathlib import Path
from urllib.parse import unquote, urlparse

from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import (
    enforce_size,
    guard_egress_host,
    require_lib,
    temp_download_path,
    validate_remote_format,
)
from webchart.services.ingest.upload import convert_to_parquet


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

        guard_egress_host(host)
        if not host or not remote_path:
            raise ConnectorError(f"Invalid FTP URI: {spec.uri}")

        try:
            async with aioftp.Client.context(host, port, user, password) as client:
                await client.download(remote_path, temp_path, write_into=True)
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            raise ConnectorError(f"FTP download failed for {spec.uri}: {e}") from e

        # aioftp에는 전송 도중 끊을 간단한 훅이 없어, 다 받은 뒤 크기 상한을 검사한다.
        try:
            enforce_size(temp_path.stat().st_size, suffix)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
