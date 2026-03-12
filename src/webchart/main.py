"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from webchart.api.middleware import RequestIdMiddleware, TimingMiddleware
from webchart.api.router import api_router
from webchart.cache.redis_cache import close_redis
from webchart.config import settings
from webchart.core.concurrency import shutdown_render_pool
from webchart.core.logging import setup_logging
from webchart.services.ingest.local import LocalIngestBackend
from webchart.services.ingest.registry import register_backend


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    setup_logging(settings.debug)

    # Ensure data directories exist
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "uploads").mkdir(exist_ok=True)
    (settings.data_dir / "datasets").mkdir(exist_ok=True)

    # Register ingest backends
    register_backend(LocalIngestBackend())

    yield

    # Shutdown
    shutdown_render_pool()
    await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(
        title="WebchartAPI",
        description="대용량 데이터 서버사이드 차트 시각화 API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TimingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # Routes
    app.include_router(api_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
