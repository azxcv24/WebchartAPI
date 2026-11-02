"""WebSocket message protocol definitions."""

from typing import Literal

from pydantic import BaseModel, Field

# --- Client → Server ---

class ViewportChangeMessage(BaseModel):
    type: str = "viewport_change"
    x_range: tuple[float, float]
    y_range: tuple[float, float] | None = None  # 무시된다: y범위는 서버가 정책으로 정한다
    zoom: int = Field(default=0, ge=0, le=24)
    dpr: Literal[1, 2, 4] | None = None  # None이면 세션의 기존 값


class HoverMessage(BaseModel):
    type: str = "hover"
    pixel_x: float
    pixel_y: float


class ScrollMessage(BaseModel):
    type: str = "scroll"
    direction: str  # left, right, up, down
    delta: float


class PingMessage(BaseModel):
    type: str = "ping"


# --- Server → Client ---

class TileInfo(BaseModel):
    tile_id: str  # "{zoom}-{x}-{y}"
    x: int
    y: int
    width: int
    height: int
    ready: bool


class TilesMetaMessage(BaseModel):
    type: str = "tiles_meta"
    tiles: list[TileInfo]


class HoverDataMessage(BaseModel):
    type: str = "hover_data"
    data_x: float
    data_y: float
    series: str
    label: str = ""
    extra: dict[str, object] | None = None


class ViewportAckMessage(BaseModel):
    type: str = "viewport_ack"
    x_range: tuple[float, float]
    zoom: int
    tile_count: int
    y_range: tuple[float, float] | None = None  # 주 y축 범위 (v1 호환)
    y_ranges: dict[str, tuple[float, float]] = Field(default_factory=dict)  # 축별 공유 y범위
    exact: bool = True


class ErrorMessage(BaseModel):
    type: str = "error"
    code: str
    message: str
