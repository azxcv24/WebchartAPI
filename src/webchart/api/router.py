"""Top-level router aggregation."""

from fastapi import APIRouter

from webchart.api.v1 import charts, datasets, sessions, ws

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(datasets.router)
api_router.include_router(charts.router)
api_router.include_router(sessions.router)
api_router.include_router(ws.router)
