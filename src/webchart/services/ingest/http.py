"""HTTP/HTTPS 인제스트 백엔드.

원격 파일을 크기 상한을 지키며 디스크로 스트리밍한 뒤 Parquet으로 변환한다.
리다이렉트는 직접 따라가며, 매 hop마다 SSRF 가드가 검사한 IP로만 접속한다.

URI:     https://host/path/file.csv
creds:   bearer | token (Authorization: Bearer ...), 또는 username/password (Basic)
options: header_<Name> 항목은 그대로 요청 헤더가 된다
"""

from pathlib import Path
from typing import Any

import httpx

from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError, WebchartError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import (
    DOWNLOAD_CHUNK_BYTES,
    CappedDownload,
    guard_egress_host,
    temp_download_path,
    validate_remote_format,
)
from webchart.services.ingest.upload import convert_to_parquet


def pinned_target(url: httpx.URL) -> tuple[httpx.URL, dict[str, str], dict[str, Any]]:
    """url의 호스트를 검사한 IP로 바꾼 (접속 URL, 추가 헤더, httpx 확장)을 돌려준다.

    Host 헤더와 TLS SNI·인증서 검사는 원래 호스트 이름을 그대로 쓴다. 그래서 서버 입장에서는
    이름으로 접속한 것과 같고, httpx가 DNS를 다시 풀지 않아 DNS 리바인딩이 통하지 않는다.
    가드가 꺼져 있으면 url을 그대로 돌려준다.
    """
    address = guard_egress_host(url.host)
    if address == url.host:
        return url, {}, {}
    extensions = {"sni_hostname": url.host} if url.scheme == "https" else {}
    return url.copy_with(host=address), {"Host": url.netloc.decode("ascii")}, extensions


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
        dataset_id = dest.name
        suffix = validate_remote_format(spec.uri)
        temp_path = temp_download_path(dataset_id, suffix)

        try:
            # 리다이렉트를 직접 따라가야 매 hop의 호스트를 guard_egress_host로 검사할 수
            # 있다 (공개 URL이 내부 주소로 리다이렉트하는 SSRF 우회 방지).
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=settings.connector_timeout
            ) as client:
                url = httpx.URL(spec.uri)
                origin = url.netloc
                for _ in range(self._MAX_REDIRECTS + 1):
                    target, pin_headers, extensions = pinned_target(url)
                    # 인증 정보는 처음 요청한 호스트에만 보낸다. 다른 호스트로 리다이렉트되면
                    # 헤더·Basic 인증을 떼어 자격 증명이 제3자에게 새지 않게 한다.
                    is_same_origin = url.netloc == origin
                    headers = self._headers(spec) if is_same_origin else {}
                    async with client.stream(
                        "GET",
                        target,
                        headers={**headers, **pin_headers},
                        auth=self._auth(spec) if is_same_origin else None,
                        extensions=extensions,
                    ) as resp:
                        if resp.status_code in self._REDIRECT_CODES:
                            location = resp.headers.get("location")
                            if not location:
                                raise ConnectorError("Redirect without Location header")
                            # 상대 경로는 IP로 바꾼 주소가 아니라 원래 URL 기준으로 푼다
                            url = url.join(location)
                            continue
                        resp.raise_for_status()
                        # Content-Length를 믿지 않고 실제로 받은 양으로 끊는다
                        with CappedDownload(temp_path, suffix) as out:
                            async for chunk in resp.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                                out.write(chunk)
                        break
                else:
                    raise ConnectorError("Too many redirects")
        except WebchartError:
            temp_path.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as e:
            temp_path.unlink(missing_ok=True)
            raise ConnectorError(f"HTTP download failed for {spec.uri}: {e}") from e

        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
