"""HTTP/HTTPS 인제스트 백엔드.

원격 파일을 크기 상한을 지키며 디스크로 스트리밍한 뒤 Parquet으로 변환한다.

URI:     https://host/path/file.csv
creds:   bearer | token (Authorization: Bearer ...), 또는 username/password (Basic)
options: header_<Name> 항목은 그대로 요청 헤더가 된다
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

_STREAM_CHUNK = 1024 * 1024  # 1 MiB


class HttpIngestBackend:
    """HTTP(S)로 파일을 내려받아 Parquet으로 변환한다."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["http", "https"]

    @staticmethod
    def _headers(spec: RemoteSourceSpec) -> dict[str, str]:
        """Bearer 토큰과 options의 header_<Name> 항목으로 요청 헤더를 만든다."""
        headers: dict[str, str] = {}
        creds = spec.credentials
        token = creds.get("bearer") or creds.get("token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for k, v in spec.options.items():
            if k.lower().startswith("header_"):
                headers[k[len("header_") :]] = v
        return headers

    @staticmethod
    def _auth(spec: RemoteSourceSpec):
        """username이 있으면 Basic 인증 튜플, 없으면 None."""
        creds = spec.credentials
        if creds.get("username"):
            return (creds["username"], creds.get("password", ""))
        return None

    _MAX_REDIRECTS = 5
    _REDIRECT_CODES = {301, 302, 303, 307, 308}

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        import httpx

        dataset_id = dest.name
        suffix = validate_remote_format(spec.uri)
        temp_path = temp_download_path(dataset_id, suffix)
        limit = size_limit_for(suffix)

        size = 0
        try:
            # 리다이렉트를 직접 따라가야 매 hop의 호스트를 guard_egress_host로 검사할 수
            # 있다 (공개 URL이 내부 주소로 리다이렉트하는 SSRF 우회 방지).
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
                                # Content-Length를 믿지 않고 실제로 받은 양으로 끊는다
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
            # 쓰는 도중의 OSError(디스크 가득 참 등)에도 쓰다 만 파일을 남기지 않는다
            temp_path.unlink(missing_ok=True)
            raise

        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
