"""원격 인제스트 커넥터 공용 유틸리티.

파일 커넥터(S3, HTTP, FTP, SFTP, SMB)는 모두 원격 객체를 로컬 임시 파일로 내려받은 뒤
convert_to_parquet을 재사용한다. 여기에는 그 공통 단계인 URI 해석, 형식 검사, 임시 경로
할당, 크기 상한, SSRF 방어, 선택 의존성 로딩이 있다.
"""

import importlib
import ipaddress
import logging
import socket
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote, urlparse

from webchart.config import settings
from webchart.core.errors import ConnectorError, UnsupportedFormatError

logger = logging.getLogger("webchart.ingest.connectors")


def _is_blocked_ip(ip: str) -> bool:
    """내부망으로 향하는 주소(사설·루프백·링크로컬·예약·멀티캐스트·미지정)이면 True다."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # 해석할 수 없는 주소는 거부한다
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def guard_egress_host(host: str | None) -> None:
    """내부망 주소로 해석되는 호스트를 거부한다.

    SSRF(클라우드 메타데이터 169.254.169.254, 내부 서비스 등) 완화용이다. 해석된 주소가
    하나라도 막힌 대역이면 거부한다. settings.block_private_egress가 False면 아무것도
    하지 않는다.
    """
    if not settings.block_private_egress:
        return
    if not host:
        raise ConnectorError("Missing host in URI")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ConnectorError(f"Cannot resolve host {host!r}: {e}") from e
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            raise ConnectorError(
                f"Refusing to fetch from private/loopback address for host {host!r} "
                f"({ip}); set WEBCHART_BLOCK_PRIVATE_EGRESS=false to allow"
            )


def remote_suffix(uri: str) -> str:
    """URI 경로의 확장자를 소문자로 돌려준다 (예: '.csv')."""
    path = urlparse(uri).path
    return Path(unquote(path)).suffix.lower()


def validate_remote_format(uri: str) -> str:
    """내려받기 전에 URI가 지원 형식을 가리키는지 확인하고 확장자를 돌려준다."""
    suffix = remote_suffix(uri)
    if suffix not in settings.allowed_upload_extensions:
        raise UnsupportedFormatError(suffix or "(none)")
    return suffix


def temp_download_path(dataset_id: str, suffix: str) -> Path:
    """원격 객체를 받아 둘 uploads/ 아래 로컬 경로. 부모 디렉터리를 만들어 둔다."""
    p = settings.data_dir / "uploads" / f"{dataset_id}{suffix}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def size_limit_for(suffix: str) -> int:
    """내려받기 크기 상한(바이트). xlsx는 청크 단위로 파싱할 수 없어 더 작게 잡는다."""
    if suffix == ".xlsx":
        return min(settings.max_upload_size, settings.max_xlsx_upload_size)
    return settings.max_upload_size


def enforce_size(num_bytes: int, suffix: str, uri: str) -> None:
    """num_bytes가 상한을 넘으면 FileTooLargeError를 낸다."""
    from webchart.core.errors import FileTooLargeError

    limit = size_limit_for(suffix)
    if num_bytes > limit:
        raise FileTooLargeError(num_bytes, limit)


def copy_capped(src, dst_path: Path, suffix: str, uri: str, chunk: int = 1 << 20) -> int:
    """파일 객체 src를 dst_path로 chunk 바이트씩 복사하고 쓴 바이트 수를 돌려준다.

    상한을 넘는 순간 멈추고, 쓰다 만 파일을 지운 뒤 FileTooLargeError를 낸다.
    """
    from webchart.core.errors import FileTooLargeError

    limit = size_limit_for(suffix)
    written = 0
    with open(dst_path, "wb") as out:
        while True:
            block = src.read(chunk)
            if not block:
                break
            written += len(block)
            if written > limit:
                dst_path.unlink(missing_ok=True)
                raise FileTooLargeError(written, limit)
            out.write(block)
    return written


def require_lib(module_name: str, extra: str) -> ModuleType:
    """선택 의존성을 import한다. 없으면 설치할 extra를 알려 주는 ConnectorError를 낸다."""
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        raise ConnectorError(
            f"'{module_name}' is required for this connector but is not installed. "
            f"Install it with: pip install 'webchart[{extra}]'"
        ) from e
