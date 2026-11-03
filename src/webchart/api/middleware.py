"""애플리케이션 미들웨어: 요청 ID, 응답 시간 기록, 호출자별 요청 수 제한."""

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
    """요청마다 짧은 ID를 붙여 로그 레코드와 `X-Request-Id` 응답 헤더로 전달한다."""

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
    """요청 처리 시간(ms)을 로그에 남기고 `X-Response-Time` 헤더로 돌려준다."""

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
    """호출자별 요청 수 제한 (메모리 내, 최근 60초 슬라이딩 윈도).

    API 키가 있으면 키별로, 없으면 클라이언트 IP별로 센다. 외부 헬스 체크가 한도를
    쓰지 않도록 /health는 제외한다. 상태가 프로세스 안에만 있으므로 워커가 여럿이면
    sticky LB와 함께 쓰거나 Redis 기반 구현으로 바꿔야 한다.
    """

    _SWEEP_EVERY = 500  # 요청 N개마다 만료된 키를 정리한다

    def __init__(self, app) -> None:
        super().__init__(app)
        self._window_s = 60.0
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._since_sweep = 0

    def _client_key(self, request: Request) -> str:
        api_key = request.headers.get(settings.api_key_header)
        # 유효한 키만 별도 버킷을 받는다. 아무 키나 붙여 IP 제한을 우회하지 못하게 한다.
        from webchart.api.auth import key_is_valid

        if api_key and settings.api_keys and key_is_valid(api_key):
            # 원본 키가 dict 키로 메모리에 남지 않도록 해시한다.
            from webchart.api.auth import hash_api_key

            return f"key:{hash_api_key(api_key)}"
        client = request.client
        return f"ip:{client.host if client else 'unknown'}"

    def _sweep(self, cutoff: float) -> None:
        """윈도가 모두 지난 키를 지운다. 락을 잡은 상태에서 호출한다."""
        stale = [k for k, b in self._hits.items() if not b or b[-1] < cutoff]
        for k in stale:
            del self._hits[k]

    async def dispatch(self, request: Request, call_next):
        if not settings.rate_limit_enabled or request.url.path == "/health":
            return await call_next(request)

        limit = settings.rate_limit_per_minute
        key = self._client_key(request)
        now = time.monotonic()
        cutoff = now - self._window_s

        with self._lock:
            self._since_sweep += 1
            if self._since_sweep >= self._SWEEP_EVERY:
                self._sweep(cutoff)
                self._since_sweep = 0

            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                # 가장 오래된 요청이 윈도를 벗어날 때까지 남은 초
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
