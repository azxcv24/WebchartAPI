"""Shared utilities for remote ingest connectors.

The file-based connectors (S3, HTTP, FTP, SFTP, SMB) all follow the same
shape: download the remote object to a local temp file, then reuse
convert_to_parquet. These helpers cover URI parsing, format validation,
temp-path allocation, and optional-dependency loading.
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
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable -> refuse
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def guard_egress_host(host: str | None) -> None:
    """Refuse hosts that resolve to private/loopback/link-local addresses.

    Mitigates SSRF (e.g. cloud metadata at 169.254.169.254, internal
    services). No-op when settings.block_private_egress is False.
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
    """Lower-cased file extension from a URI path (e.g. '.csv')."""
    path = urlparse(uri).path
    return Path(unquote(path)).suffix.lower()


def validate_remote_format(uri: str) -> str:
    """Ensure the URI points at a supported file type before downloading."""
    suffix = remote_suffix(uri)
    if suffix not in settings.allowed_upload_extensions:
        raise UnsupportedFormatError(suffix or "(none)")
    return suffix


def temp_download_path(dataset_id: str, suffix: str) -> Path:
    """Local path under uploads/ to stream a remote object into."""
    p = settings.data_dir / "uploads" / f"{dataset_id}{suffix}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def size_limit_for(suffix: str) -> int:
    """Download size cap; xlsx is tighter because parsing is not chunked."""
    if suffix == ".xlsx":
        return min(settings.max_upload_size, settings.max_xlsx_upload_size)
    return settings.max_upload_size


def enforce_size(num_bytes: int, suffix: str, uri: str) -> None:
    """Raise FileTooLargeError if a downloaded object exceeds the cap."""
    from webchart.core.errors import FileTooLargeError

    limit = size_limit_for(suffix)
    if num_bytes > limit:
        raise FileTooLargeError(num_bytes, limit)


def copy_capped(src, dst_path: Path, suffix: str, uri: str, chunk: int = 1 << 20) -> int:
    """Stream a file-like `src` to dst_path, aborting if the size cap is hit.

    Deletes the partial file and raises FileTooLargeError on overflow.
    Returns the number of bytes written.
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
    """Import an optional connector dependency or raise a clear ConnectorError."""
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        raise ConnectorError(
            f"'{module_name}' is required for this connector but is not installed. "
            f"Install it with: pip install 'webchart[{extra}]'"
        ) from e
