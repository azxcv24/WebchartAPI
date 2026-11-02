from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from webchart.models.chart import ChartSpec


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ViewportWindow(BaseModel):
    """클라이언트 화면 상태. x_range는 정규 좌표(시간축은 epoch 나노초)다.

    y_range는 서버가 프레임마다 확정한 값을 저장한다. 호버의 픽셀 → 데이터 변환이 실제
    타일과 같은 y 스케일을 쓰게 하기 위해서다.
    """

    x_range: tuple[float, float]
    y_range: tuple[float, float] | None = None
    zoom: int = Field(default=0, ge=0, le=24)
    dpr: Literal[1, 2, 4] = 1
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
    owner_key_hash: str | None = None  # sha256 of the creating API key
    created_at: datetime = Field(default_factory=_utcnow)
    last_active: datetime = Field(default_factory=_utcnow)


class SessionResponse(BaseModel):
    session_id: str
    dataset_id: str
    websocket_url: str
    viewport: ViewportWindow
    created_at: datetime
