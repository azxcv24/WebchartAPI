"""FastAPI 애플리케이션 팩토리와 lifespan(시작·종료 처리)."""

import importlib.metadata
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from webchart.api import viewer
from webchart.api.middleware import RateLimitMiddleware, RequestIdMiddleware, TimingMiddleware
from webchart.api.router import api_router
from webchart.cache.redis_cache import close_redis, init_redis
from webchart.config import settings
from webchart.core.concurrency import init_render_pool, shutdown_render_pool
from webchart.core.logging import setup_logging
from webchart.core.metrics import metrics
from webchart.services.ingest.clickhouse import ClickHouseIngestBackend
from webchart.services.ingest.ftp import FtpIngestBackend
from webchart.services.ingest.http import HttpIngestBackend
from webchart.services.ingest.local import LocalIngestBackend
from webchart.services.ingest.registry import register_backend
from webchart.services.ingest.s3 import S3IngestBackend
from webchart.services.ingest.sftp import SftpIngestBackend
from webchart.services.ingest.smb import SmbIngestBackend


@asynccontextmanager
async def lifespan(app: FastAPI):
    """시작 시 로깅·디렉터리·인제스트 백엔드·렌더 풀·Redis를 준비하고, 종료 시 정리한다."""
    setup_logging(settings.debug)

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "uploads").mkdir(exist_ok=True)
    (settings.data_dir / "datasets").mkdir(exist_ok=True)

    # 커넥터는 드라이버를 지연 import하므로 [connectors] extra가 없어도 등록 비용이 작다.
    # 드라이버가 없으면 실제로 쓸 때에야 명확한 에러를 낸다.
    for backend in (
        LocalIngestBackend(),
        S3IngestBackend(),
        HttpIngestBackend(),
        FtpIngestBackend(),
        SftpIngestBackend(),
        SmbIngestBackend(),
        ClickHouseIngestBackend(),
    ):
        register_backend(backend)

    # 첫 요청끼리 초기화 경쟁을 하지 않도록 싱글턴을 미리 만든다.
    init_render_pool()
    await init_redis()

    yield

    shutdown_render_pool()
    await close_redis()


def _package_version() -> str:
    """설치된 패키지 버전 (pyproject.toml). 설치하지 않고 소스로 돌리면 "dev"."""
    try:
        return importlib.metadata.version("webchart")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def create_app() -> FastAPI:
    app = FastAPI(
        title="WebchartAPI",
        description="대용량 데이터 서버사이드 차트 시각화 API",
        version=_package_version(),
        lifespan=lifespan,
    )

    # add_middleware는 바깥쪽에 쌓이므로 RequestId가 가장 먼저 실행된다.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TimingMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(api_router)
    app.include_router(viewer.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    if settings.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        async def prometheus_metrics() -> PlainTextResponse:
            """캐시 적중률, 렌더 지연, 경로별 exact 비율 (Prometheus 텍스트 형식)."""
            return PlainTextResponse(
                metrics.render_prometheus(), media_type="text/plain; version=0.0.4"
            )

    return app


app = create_app()
