"""WebSocket 프로토콜 v2 메시지 (계획 문서 4.3).

모든 요청에 클라이언트가 붙인 `req_id`가 있고, 서버 응답은 같은 req_id를 돌려준다.
새 viewport 요청이 오면 진행 중인 이전 프레임은 취소된다 (latest-wins). 클라이언트는
최신 req_id가 아닌 응답을 버리면 된다.

타일 이미지는 바이너리 프레임으로 보낸다: 고정 헤더 13바이트(big-endian) + 이미지.

    [magic u8 = 0x57 'W'][version u8 = 2][req_id u32][zoom u8][index u32][dpr u8][format u8]

format: 0 = PNG
"""

from typing import Literal

from pydantic import BaseModel, Field

TILE_HEADER_FORMAT = ">BBIBIBB"
TILE_HEADER_MAGIC = 0x57
PROTOCOL_VERSION = 2
TILE_FORMAT_PNG = 0

ReqId = int


# --- 클라이언트 → 서버 ---


class ViewportRequest(BaseModel):
    """화면 상태가 바뀜: 새 프레임을 그려 달라는 요청.

    zoom을 생략하면 서버가 화면 폭에 맞춰 고른다 (타일 픽셀 ≥ 화면 픽셀이 되는 가장 얕은 줌).
    """

    type: Literal["viewport"] = "viewport"
    req_id: ReqId = Field(ge=0, le=2**32 - 1)
    x_range: tuple[float, float]
    zoom: int | None = Field(default=None, ge=0, le=24)
    width_px: int = Field(default=1200, ge=16, le=16384)
    height_px: int = Field(default=600, ge=16, le=16384)
    dpr: Literal[1, 2, 4] = 1


class HoverRequest(BaseModel):
    """마우스 위치(뷰포트 기준 픽셀)의 크로스헤어 값 요청."""

    type: Literal["hover"] = "hover"
    req_id: ReqId = Field(ge=0, le=2**32 - 1)
    pixel_x: float
    pixel_y: float = 0.0


class SelectRequest(BaseModel):
    """x 구간 [start, end)의 정확한 통계 요청."""

    type: Literal["select"] = "select"
    req_id: ReqId = Field(ge=0, le=2**32 - 1)
    x_range: tuple[float, float]


class PingRequest(BaseModel):
    type: Literal["ping"] = "ping"


# --- 서버 → 클라이언트 ---


class TickMessage(BaseModel):
    value: float
    label: str
    pixel: float  # 뷰포트 왼쪽(x) 또는 위쪽(y)에서의 거리 (CSS px)


class TileRef(BaseModel):
    zoom: int
    index: int
    x_start: float  # 타일이 덮는 x 구간 (정규 좌표)
    x_end: float
    visible: bool


class FrameBegin(BaseModel):
    """프레임 시작: 이 프레임의 타일 목록, 축별 y범위, 서버가 계산한 눈금."""

    type: Literal["frame_begin"] = "frame_begin"
    req_id: ReqId
    zoom: int
    dpr: int
    tile_size_px: int  # 장치 픽셀 (= 설정 tile_size × dpr)
    domain: tuple[float, float]  # 타일 격자의 x 전체 범위. 실시간 추가로 넓어질 수 있다
    x_range: tuple[float, float]
    tiles: list[TileRef]
    y_ranges: dict[str, tuple[float, float]]
    x_ticks: list[TickMessage]
    y_ticks: dict[str, list[TickMessage]]


class FrameEnd(BaseModel):
    type: Literal["frame_end"] = "frame_end"
    req_id: ReqId
    sent: int
    cached: int
    exact: bool
    elapsed_ms: float


class CrosshairValue(BaseModel):
    series: str
    x: float
    y: float
    label: str


class Crosshair(BaseModel):
    type: Literal["crosshair"] = "crosshair"
    req_id: ReqId
    x: float
    points: list[CrosshairValue]


class SeriesStats(BaseModel):
    count: int
    min: float | None
    max: float | None
    mean: float | None
    first: float | None
    last: float | None
    min_x: float | None
    max_x: float | None


class RangeStats(BaseModel):
    type: Literal["range_stats"] = "range_stats"
    req_id: ReqId
    x_range: tuple[float, float]
    exact: bool
    source: str
    series: dict[str, SeriesStats]


class DataAppendedMessage(BaseModel):
    """데이터가 추가되었다. x_range와 겹치는 화면이면 viewport를 다시 보내면 된다.

    바뀌지 않은 타일은 서버 캐시 키가 그대로라 다시 보내도 캐시에서 바로 나온다.
    x_range가 None이면 전체가 바뀐 것이다. domain은 새 격자 범위다.
    """

    type: Literal["data_appended"] = "data_appended"
    generation: int
    x_range: tuple[float, float] | None
    domain: tuple[float, float]


class Pong(BaseModel):
    type: Literal["pong"] = "pong"


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    req_id: ReqId | None = None
    code: str
    message: str
