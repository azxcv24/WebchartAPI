"""URI 스킴으로 인제스트 백엔드를 찾는 레지스트리."""

import logging
from urllib.parse import urlparse

from webchart.core.errors import ProtocolNotSupportedError
from webchart.services.ingest.base import IngestBackend

logger = logging.getLogger("webchart.ingest.registry")

_backends: dict[str, IngestBackend] = {}


def register_backend(backend: IngestBackend) -> None:
    """백엔드를 지원 스킴마다 등록한다. 같은 스킴은 나중 등록이 덮어쓴다."""
    for protocol in backend.supported_protocols:
        _backends[protocol.lower()] = backend
        logger.info(f"Registered ingest backend for protocol: {protocol}")


def get_backend(uri: str) -> IngestBackend:
    """URI 스킴에 맞는 백엔드를 돌려준다. 없으면 ProtocolNotSupportedError."""
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()

    if scheme not in _backends:
        raise ProtocolNotSupportedError(scheme)

    return _backends[scheme]
