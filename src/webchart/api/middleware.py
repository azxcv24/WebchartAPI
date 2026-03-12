"""Application middleware."""

import time
import uuid
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("webchart.middleware")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Add unique request ID to each request."""

    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        response: Response = await call_next(request)
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
