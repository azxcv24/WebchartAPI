"""Application middleware."""

import logging
import threading
import time
import uuid
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from webchart.config import settings
from webchart.core.logging import request_id_var

logger = logging.getLogger("webchart.middleware")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Add unique request ID to each request and propagate it to log records."""

    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response: Response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response


class TimingMiddleware(BaseHTTPMiddleware):
    """Log request timing."""

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response: Response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000

        logger.info(
            f"{request.method} {request.url.path} -> {response.status_code} ({elapsed:.1f}ms)"
        )
        response.headers["X-Response-Time"] = f"{elapsed:.1f}ms"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-caller fixed-window rate limit (in-memory).

    Keyed by API key when present, otherwise by client IP. /health is
    exempt so external probes do not consume budget. This is process-local;
    deployments with multiple workers should pair with a sticky LB or
    swap in a Redis-backed implementation.
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self._window_s = 60.0
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def _client_key(self, request: Request) -> str:
        api_key = request.headers.get(settings.api_key_header)
        if api_key:
            return f"key:{api_key}"
        client = request.client
        return f"ip:{client.host if client else 'unknown'}"

    async def dispatch(self, request: Request, call_next):
        if not settings.rate_limit_enabled or request.url.path == "/health":
            return await call_next(request)

        limit = settings.rate_limit_per_minute
        key = self._client_key(request)
        now = time.monotonic()
        cutoff = now - self._window_s

        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(self._window_s - (now - bucket[0])))
                return JSONResponse(
                    status_code=429,
                    content={
                        "code": "RATE_LIMITED",
                        "message": f"Limit {limit}/min exceeded",
                    },
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)

        return await call_next(request)
