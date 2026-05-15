from datetime import datetime, timezone

from pydantic import BaseModel, Field

from webchart.models.chart import ChartSpec


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ViewportWindow(BaseModel):
    x_range: tuple[float, float]
    y_range: tuple[float, float] | None = None
    zoom: int = Field(default=0, ge=0, le=24)
    pixel_width: int = Field(default=1200, ge=64, le=8192)
    pixel_height: int = Field(default=600, ge=64, le=8192)


class SessionCreate(BaseModel):
    dataset_id: str
    chart_config: ChartSpec
    viewport: ViewportWindow


class SessionState(BaseModel):
    session_id: str
    dataset_id: str
    chart_config: ChartSpec
    viewport: ViewportWindow
    created_at: datetime = Field(default_factory=_utcnow)
    last_active: datetime = Field(default_factory=_utcnow)


class SessionResponse(BaseModel):
    session_id: str
    dataset_id: str
    websocket_url: str
    viewport: ViewportWindow
    created_at: datetime
