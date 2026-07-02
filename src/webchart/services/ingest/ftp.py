"""FTP ingest backend (aioftp).

URI:    ftp://host[:port]/path/file.csv
creds:  username, password  (defaults to anonymous)
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

logger = logging.getLogger("webchart.ingest.ftp")


class FtpIngestBackend:
    """Download a file over FTP and convert it to Parquet."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["ftp"]

    async def validate_source(self, spec: RemoteSourceSpec) -> bool:
        try:
            validate_remote_format(spec.uri)
            return True
        except Exception:
            return False

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

        if not host or not remote_path:
            raise ConnectorError(f"Invalid FTP URI: {spec.uri}")

        try:
            async with aioftp.Client.context(host, port, user, password) as client:
                await client.download(remote_path, temp_path, write_into=True)
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            raise ConnectorError(f"FTP download failed for {spec.uri}: {e}") from e

        # Post-hoc size cap (aioftp has no simple mid-stream limit hook).
        try:
            enforce_size(temp_path.stat().st_size, suffix, spec.uri)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
