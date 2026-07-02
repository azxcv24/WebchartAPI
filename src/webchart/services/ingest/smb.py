"""SMB / CIFS ingest backend (smbprotocol's smbclient).

URI:    smb://host/share/path/file.csv
creds:  username, password, [domain]
options: port (default 445)
"""

import logging
from pathlib import Path
from urllib.parse import unquote, urlparse

from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError, WebchartError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import (
    copy_capped,
    require_lib,
    temp_download_path,
    validate_remote_format,
)
from webchart.services.ingest.upload import convert_to_parquet

logger = logging.getLogger("webchart.ingest.smb")


class SmbIngestBackend:
    """Download a file from an SMB share and convert it to Parquet."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["smb"]

    @staticmethod
    def _unc_path(uri: str) -> str:
        # smb://host/share/dir/file.csv  ->  \\host\share\dir\file.csv
        parsed = urlparse(uri)
        host = parsed.hostname
        path = unquote(parsed.path)
        if not host or not path.strip("/"):
            raise ConnectorError(f"Invalid SMB URI (need smb://host/share/path): {uri}")
        return "\\\\" + host + path.replace("/", "\\")

    async def validate_source(self, spec: RemoteSourceSpec) -> bool:
        try:
            validate_remote_format(spec.uri)
            self._unc_path(spec.uri)
            return True
        except Exception:
            return False

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        smbclient = require_lib("smbclient", "connectors")

        dataset_id = dest.name
        suffix = validate_remote_format(spec.uri)
        temp_path = temp_download_path(dataset_id, suffix)
        unc = self._unc_path(spec.uri)

        creds = spec.credentials
        open_kwargs: dict = {}
        if creds.get("username"):
            open_kwargs["username"] = creds["username"]
        if creds.get("password"):
            open_kwargs["password"] = creds["password"]
        if creds.get("domain"):
            # smbprotocol expects DOMAIN\\user; prepend if not already qualified
            user = open_kwargs.get("username", "")
            if user and "\\" not in user:
                open_kwargs["username"] = f"{creds['domain']}\\{user}"
        if spec.options.get("port"):
            open_kwargs["port"] = int(spec.options["port"])

        def _download() -> None:
            try:
                with smbclient.open_file(unc, mode="rb", **open_kwargs) as src:
                    copy_capped(src, temp_path, suffix, spec.uri)
            except WebchartError:
                temp_path.unlink(missing_ok=True)
                raise
            except Exception as e:
                temp_path.unlink(missing_ok=True)
                raise ConnectorError(f"SMB download failed for {spec.uri}: {e}") from e

        await run_in_thread(_download)
        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
