"""Shared utilities for remote ingest connectors.

The file-based connectors (S3, HTTP, FTP, SFTP, SMB) all follow the same
shape: download the remote object to a local temp file, then reuse
convert_to_parquet. These helpers cover URI parsing, format validation,
temp-path allocation, and optional-dependency loading.
"""

import importlib
import logging
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote, urlparse

from webchart.config import settings
from webchart.core.errors import ConnectorError, UnsupportedFormatError

logger = logging.getLogger("webchart.ingest.connectors")


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


def require_lib(module_name: str, extra: str) -> ModuleType:
    """Import an optional connector dependency or raise a clear ConnectorError."""
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        raise ConnectorError(
            f"'{module_name}' is required for this connector but is not installed. "
            f"Install it with: pip install 'webchart[{extra}]'"
        ) from e
