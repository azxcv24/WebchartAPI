"""SFTP ingest backend (asyncssh).

URI:    sftp://host[:port]/path/file.csv
creds:  username, password  and/or  private_key (PEM text), passphrase
options: known_hosts — host-key verification is ON by default; set a path
         to a known_hosts file, or "insecure" to explicitly disable it
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
    """Download a file over SFTP and convert it to Parquet."""

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

        # Host-key verification is ON by default (uses the system known_hosts).
        # options.known_hosts:
        #   - a path string -> verify against that known_hosts file
        #   - "insecure"    -> explicit opt-out (disables verification)
        #   - unset/other   -> default asyncssh verification
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

        # Post-hoc size cap.
        try:
            enforce_size(temp_path.stat().st_size, suffix, spec.uri)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
