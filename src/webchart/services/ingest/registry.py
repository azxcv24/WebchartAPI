"""Protocol dispatcher for data ingestion backends."""

import logging
from urllib.parse import urlparse

from webchart.core.errors import ProtocolNotSupportedError
from webchart.services.ingest.base import IngestBackend

logger = logging.getLogger("webchart.ingest.registry")

_backends: dict[str, IngestBackend] = {}


def register_backend(backend: IngestBackend) -> None:
    """Register an ingest backend for its supported protocols."""
    for protocol in backend.supported_protocols:
        _backends[protocol.lower()] = backend
        logger.info(f"Registered ingest backend for protocol: {protocol}")


def get_backend(uri: str) -> IngestBackend:
    """Get the appropriate backend for a URI."""
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()

    if scheme not in _backends:
        raise ProtocolNotSupportedError(scheme)

    return _backends[scheme]


