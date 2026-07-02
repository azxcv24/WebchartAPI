"""HTTP/HTTPS ingest backend.

Streams a remote file to disk with a size cap, then converts to Parquet.

URI:    https://host/path/file.csv
creds:  bearer | token (Authorization: Bearer ...), or username/password (Basic)
options: header_<Name> entries become request headers
"""

import logging
from pathlib import Path
from urllib.parse import urlparse

from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError, FileTooLargeError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import (
    guard_egress_host,
    size_limit_for,
    temp_download_path,
    validate_remote_format,
)
from webchart.services.ingest.upload import convert_to_parquet

logger = logging.getLogger("webchart.ingest.http")

_STREAM_CHUNK = 1024 * 1024  # 1MB


class HttpIngestBackend:
    """Download a file over HTTP(S) and convert it to Parquet."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["http", "https"]

    @staticmethod
    def _headers(spec: RemoteSourceSpec) -> dict[str, str]:
        headers: dict[str, str] = {}
        creds = spec.credentials
        token = creds.get("bearer") or creds.get("token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for k, v in spec.options.items():
            if k.lower().startswith("header_"):
                headers[k[len("header_"):]] = v
        return headers

    @staticmethod
    def _auth(spec: RemoteSourceSpec):
        creds = spec.credentials
        if creds.get("username"):
            return (creds["username"], creds.get("password", ""))
        return None

    _MAX_REDIRECTS = 5
    _REDIRECT_CODES = {301, 302, 303, 307, 308}

    async def validate_source(self, spec: RemoteSourceSpec) -> bool:
        import httpx

        try:
            guard_egress_host(urlparse(spec.uri).hostname)
            # follow_redirects disabled: we validate each hop's host ourselves
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=settings.connector_timeout
            ) as client:
                r = await client.head(
                    spec.uri, headers=self._headers(spec), auth=self._auth(spec)
                )
                return r.status_code < 400 or r.status_code in self._REDIRECT_CODES
        except Exception as e:
            logger.debug(f"HTTP validate_source failed: {e}")
            return False

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        import httpx

        dataset_id = dest.name
        suffix = validate_remote_format(spec.uri)
        temp_path = temp_download_path(dataset_id, suffix)
        limit = size_limit_for(suffix)

        size = 0
        try:
            # follow_redirects disabled so we can guard every redirect target.
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=settings.connector_timeout
            ) as client:
                url = spec.uri
                for _ in range(self._MAX_REDIRECTS + 1):
                    guard_egress_host(urlparse(url).hostname)
                    async with client.stream(
                        "GET", url, headers=self._headers(spec), auth=self._auth(spec)
                    ) as resp:
                        if resp.status_code in self._REDIRECT_CODES:
                            location = resp.headers.get("location")
                            if not location:
                                raise ConnectorError("Redirect without Location header")
                            url = str(resp.url.join(location))
                            continue
                        resp.raise_for_status()
                        with open(temp_path, "wb") as f:
                            async for chunk in resp.aiter_bytes(_STREAM_CHUNK):
                                size += len(chunk)
                                if size > limit:
                                    raise FileTooLargeError(size, limit)
                                f.write(chunk)
                        break
                else:
                    raise ConnectorError("Too many redirects")
        except FileTooLargeError:
            temp_path.unlink(missing_ok=True)
            raise
        except ConnectorError:
            temp_path.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as e:
            temp_path.unlink(missing_ok=True)
            raise ConnectorError(f"HTTP download failed for {spec.uri}: {e}") from e
        except Exception:
            # e.g. OSError mid-write (disk full) — don't leak the partial file
            temp_path.unlink(missing_ok=True)
            raise

        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
