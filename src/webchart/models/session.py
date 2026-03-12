from datetime import datetime

from pydantic import BaseModel, Field

from webchart.models.chart import ChartSpec


class ViewportWindow(BaseModel):
    x_range: tuple[float, float]
    y_range: tuple[float, float] | None = None
    zoom: int = 0
    pixel_width: int = 1200
    pixel_height: int = 600


class SessionCreate(BaseModel):
    dataset_id: str
    chart_config: ChartSpec
    viewport: ViewportWindow


class SessionState(BaseModel):
    session_id: str
    dataset_id: str
    chart_config: ChartSpec
    viewport: ViewportWindow
    created_at: datetime = Field(default_factory=datetime.now)
    last_active: datetime = Field(default_factory=datetime.now)


class SessionResponse(BaseModel):
    session_id: str
    dataset_id: str
    websocket_url: str
    viewport: ViewportWindow
    created_at: datetime
