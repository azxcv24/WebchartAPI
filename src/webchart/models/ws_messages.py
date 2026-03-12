"""WebSocket message protocol definitions."""

from pydantic import BaseModel


# --- Client → Server ---

class ViewportChangeMessage(BaseModel):
    type: str = "viewport_change"
    x_range: tuple[float, float]
    y_range: tuple[float, float] | None = None
    zoom: int = 0


class HoverMessage(BaseModel):
    type: str = "hover"
    pixel_x: float
    pixel_y: float


class ClickMessage(BaseModel):
    type: str = "click"
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


class OverlayMessage(BaseModel):
    type: str = "overlay"
    base64_png: str
    x: int
    y: int
    width: int
    height: int


class ViewportAckMessage(BaseModel):
    type: str = "viewport_ack"
    x_range: tuple[float, float]
    zoom: int
    tile_count: int


class ErrorMessage(BaseModel):
    type: str = "error"
    code: str
    message: str
