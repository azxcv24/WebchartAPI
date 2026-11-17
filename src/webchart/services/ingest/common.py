"""원격 인제스트 커넥터 공용 유틸리티.

파일 커넥터(S3, HTTP, FTP, SFTP, SMB)는 모두 원격 객체를 로컬 임시 파일로 내려받은 뒤
convert_to_parquet을 재사용한다. 여기에는 그 공통 단계인 URI 해석, 형식 검사, 임시 경로
할당, 크기 상한, SSRF 방어, 선택 의존성 로딩이 있다.
"""

import importlib
import ipaddress
import socket
from pathlib import Path
from types import ModuleType, TracebackType
from typing import BinaryIO, Protocol
from urllib.parse import unquote, urlparse

from webchart.config import settings
from webchart.core.errors import ConnectorError, FileTooLargeError, UnsupportedFormatError

DOWNLOAD_CHUNK_BYTES = 1 << 20  # 1 MiB


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


def guard_egress_host(host: str | None) -> str:
    """호스트를 한 번 해석해 검사하고, 접속할 IP 주소를 돌려준다.

    SSRF(클라우드 메타데이터 169.254.169.254, 내부 서비스 등) 완화용이다. 해석된 주소가
    하나라도 막힌 대역이면 거부한다.

    커넥터는 돌려받은 주소로 접속해야 한다. 검사와 접속이 DNS를 따로 풀면, 그 사이에
    답을 내부 주소로 바꾸는 DNS 리바인딩으로 검사를 건너뛸 수 있기 때문이다.
    settings.block_private_egress가 False면 검사하지 않고 host를 그대로 돌려준다.
    """
    if not settings.block_private_egress:
        return host or ""
    if not host:
        raise ConnectorError("Missing host in URI")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ConnectorError(f"Cannot resolve host {host!r}: {e}") from e
    addresses = [str(info[4][0]) for info in infos]
    for ip in addresses:
        if _is_blocked_ip(ip):
            raise ConnectorError(
                f"Refusing to fetch from private/loopback address for host {host!r} "
                f"({ip}); set WEBCHART_BLOCK_PRIVATE_EGRESS=false to allow"
            )
    if not addresses:
        raise ConnectorError(f"Cannot resolve host {host!r}")
    return addresses[0]


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


def enforce_size(num_bytes: int, suffix: str) -> None:
    """num_bytes가 상한을 넘으면 FileTooLargeError를 낸다 (원격이 알려 준 크기로 미리 거를 때)."""
    limit = size_limit_for(suffix)
    if num_bytes > limit:
        raise FileTooLargeError(num_bytes, limit)


class _Readable(Protocol):
    def read(self, size: int, /) -> bytes: ...


class CappedDownload:
    """원격 파일을 크기 상한을 지키며 로컬 파일에 쓴다.

    with 블록 안에서 받은 조각을 write()로 넘긴다. 쓴 양이 상한을 넘는 순간
    FileTooLargeError를 내므로, 원격이 크기를 속이거나 알려 주지 않아도 디스크를
    상한 이상 쓰지 않는다. 블록이 예외로 끝나면 쓰다 만 파일을 지운다.

        with CappedDownload(temp_path, suffix) as out:
            async for block in stream:
                out.write(block)
    """

    def __init__(self, dst_path: Path, suffix: str) -> None:
        self.path = dst_path
        self.limit_bytes = size_limit_for(suffix)
        self.written_bytes = 0
        self._out: BinaryIO | None = None

    def __enter__(self) -> "CappedDownload":
        self._out = open(self.path, "wb")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._out is not None:
            self._out.close()
        if exc_type is not None:
            self.path.unlink(missing_ok=True)

    def write(self, block: bytes) -> None:
        """조각을 쓴다. 상한을 넘기면 쓰지 않고 FileTooLargeError를 낸다."""
        if self._out is None:
            raise RuntimeError("CappedDownload.write() outside of a with block")
        self.written_bytes += len(block)
        if self.written_bytes > self.limit_bytes:
            raise FileTooLargeError(self.written_bytes, self.limit_bytes)
        self._out.write(block)

    def copy_from(self, src: _Readable, chunk_bytes: int = DOWNLOAD_CHUNK_BYTES) -> None:
        """동기 파일 객체 src를 끝까지 읽어 쓴다 (SMB, S3 본문)."""
        while block := src.read(chunk_bytes):
            self.write(block)


def require_lib(module_name: str, extra: str) -> ModuleType:
    """선택 의존성을 import한다. 없으면 설치할 extra를 알려 주는 ConnectorError를 낸다."""
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        raise ConnectorError(
            f"'{module_name}' is required for this connector but is not installed. "
            f"Install it with: pip install 'webchart[{extra}]'"
        ) from e
