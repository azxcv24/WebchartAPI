"""차트 API: 정적 렌더, 차트 등록, 임베드 이미지, XYZ 타일 (계획 문서 4.2)."""

import hashlib
import re
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel

from webchart.api.auth import key_is_valid
from webchart.application.chart_registry import (
    embed_token,
    is_valid_token,
    load_chart,
    register_chart,
)
from webchart.application.static_chart import RenderedChart, render_static_chart
from webchart.application.tiles import (
    FramePlan,
    RenderedTile,
    TileRequest,
    default_tile_renderer,
    plan_frame,
)
from webchart.application.windows import lattice_for
from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.errors import WebchartError, webchart_error_to_http
from webchart.domain.geometry import MAX_ZOOM, TileAddress
from webchart.models.chart import ChartSpec, ImageFormat, RenderResult
from webchart.models.dataset import DatasetMeta
from webchart.services.data.access import get_dataset_meta

router = APIRouter(prefix="/charts", tags=["charts"])

_MIME_TYPES = {
    ImageFormat.PNG: "image/png",
    ImageFormat.SVG: "image/svg+xml",
    ImageFormat.WEBP: "image/webp",
}


async def _render(spec: ChartSpec) -> RenderedChart:
    try:
        return await run_in_thread(render_static_chart, spec)
    except WebchartError as error:
        raise webchart_error_to_http(error) from error


def _provenance_headers(rendered: RenderedChart) -> dict[str, str]:
    """그림이 원본 전체 렌더와 같은지(exact)와 어디서 읽었는지(source)를 알린다 (P3)."""
    return {
        "X-Render-Id": str(uuid.uuid4()),
        "X-Data-Points": str(rendered.point_count),
        "X-Webchart-Exact": "true" if rendered.is_exact else "false",
        "X-Webchart-Source": rendered.source,
    }


@router.post("/render")
async def render_chart(spec: ChartSpec) -> Response:
    """차트 이미지를 그린다. 응답 헤더에 exact/source가 붙는다."""
    rendered = await _render(spec)
    return Response(
        content=rendered.image,
        media_type=_MIME_TYPES[spec.format],
        headers=_provenance_headers(rendered),
    )


@router.post("/render/info", response_model=RenderResult)
async def render_chart_info(spec: ChartSpec) -> RenderResult:
    """차트를 그리고 이미지 대신 메타데이터만 반환한다."""
    rendered = await _render(spec)
    return RenderResult(
        render_id=str(uuid.uuid4()),
        format=spec.format,
        width=spec.width,
        height=spec.height,
        size_bytes=len(rendered.image),
        is_exact=rendered.is_exact,
        source=rendered.source,
        point_count=rendered.point_count,
    )


# --- 차트 등록 + 임베드 (계획 문서 4.2) -----------------------------------------------


class RegisteredChart(BaseModel):
    chart_id: str
    image_url: str  # `<img src>` 한 줄로 쓰는 URL (토큰 포함)
    tile_url_template: str  # {z}, {i}, {dpr}를 채워 쓰는 XYZ 타일 URL
    token: str | None  # 인증이 켜져 있을 때 임베드 URL에 붙는 토큰


@router.post("", response_model=RegisteredChart, status_code=201)
async def register(spec: ChartSpec) -> RegisteredChart:
    """차트 spec을 등록한다. 같은 spec은 같은 chart_id다."""
    chart_id = register_chart(spec)
    token = embed_token(chart_id)
    query = f"?token={token}" if token else ""
    base = f"/api/v1/charts/{chart_id}"
    return RegisteredChart(
        chart_id=chart_id,
        image_url=f"{base}/image.{spec.format.value}{query}",
        tile_url_template=f"{base}/tiles/{{z}}/{{i}}@{{dpr}}x.png{query}",
        token=token,
    )


@router.get("/{chart_id}", response_model=ChartSpec)
async def get_chart(chart_id: str) -> ChartSpec:
    try:
        return load_chart(chart_id)
    except WebchartError as error:
        raise webchart_error_to_http(error) from error


# 임베드 라우터는 전역 API 키 의존성을 거치지 않는다. 헤더 키 또는 차트별 토큰으로 인증한다.
embed_router = APIRouter(prefix="/charts", tags=["embed"])

# 임베드 응답의 브라우저·CDN 캐시 시간. 데이터가 재인제스트되면 ETag가 바뀐다.
EMBED_MAX_AGE_S = 300

_TILE_NAME = re.compile(r"^(?P<index>\d+)@(?P<dpr>[124])x\.png$")


def _authorize_embed(request: Request, chart_id: str, token: str | None) -> ChartSpec:
    has_key = key_is_valid(request.headers.get(settings.api_key_header))
    if not (has_key or is_valid_token(chart_id, token)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "API 키 또는 차트 토큰이 필요하다"},
        )
    try:
        return load_chart(chart_id)
    except WebchartError as error:
        raise webchart_error_to_http(error) from error


def _cached_response(request: Request, etag: str, build: Callable[[], Response]) -> Response:
    """If-None-Match가 같으면 304, 아니면 ETag·Cache-Control을 붙여 응답한다."""
    quoted = f'"{etag}"'
    headers = {"ETag": quoted, "Cache-Control": f"public, max-age={EMBED_MAX_AGE_S}"}
    if request.headers.get("if-none-match") == quoted:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    response = build()
    response.headers.update(headers)
    return response


@embed_router.get("/{chart_id}/image.{extension}")
async def chart_image(
    request: Request,
    chart_id: str,
    extension: ImageFormat,
    x0: float | None = None,
    x1: float | None = None,
    w: Annotated[int | None, Query(ge=64, le=8192)] = None,
    h: Annotated[int | None, Query(ge=64, le=8192)] = None,
    token: str | None = None,
) -> Response:
    """등록된 차트를 이미지 하나로 (`<img src>` 임베드). x0/x1은 정규 좌표 x범위."""
    spec = _authorize_embed(request, chart_id, token)
    overrides: dict[str, Any] = {"format": extension}
    if w is not None:
        overrides["width"] = w
    if h is not None:
        overrides["height"] = h
    if x0 is not None and x1 is not None:
        overrides["x_axis"] = spec.x_axis.model_copy(update={"range": (x0, x1)})
    spec = spec.model_copy(update=overrides)
    meta = await run_in_thread(get_dataset_meta, spec.dataset_id)
    etag = hashlib.sha256(
        f"{chart_id}:{meta.data_version}:{meta.generation}:{x0}:{x1}:{w}:{h}:{extension.value}".encode()
    ).hexdigest()[:24]

    rendered: list[RenderedChart] = []
    if request.headers.get("if-none-match") != f'"{etag}"':
        rendered.append(await _render(spec))

    def build() -> Response:
        return Response(
            content=rendered[0].image,
            media_type=_MIME_TYPES[extension],
            headers=_provenance_headers(rendered[0]),
        )

    return _cached_response(request, etag, build)


@embed_router.get("/{chart_id}/tiles/{zoom}/{tile_name}")
async def chart_tile(
    request: Request,
    chart_id: str,
    zoom: Annotated[int, Path(ge=0, le=MAX_ZOOM)],
    tile_name: str,
    token: str | None = None,
) -> Response:
    """XYZ 타일: `/tiles/{z}/{i}@{dpr}x.png`. 리버스 프록시·CDN 캐시에 그대로 올릴 수 있다.

    y범위는 줌마다 고정이다 (프레임이 없으므로 화면 전체 = x 전체로 계획한다).
    """
    matched = _TILE_NAME.match(tile_name)
    if matched is None:
        raise HTTPException(
            status_code=404, detail={"code": "TILE_NOT_FOUND", "message": tile_name}
        )
    spec = _authorize_embed(request, chart_id, token)
    index, dpr = int(matched["index"]), int(matched["dpr"])
    if index >= 1 << zoom:
        raise HTTPException(
            status_code=404, detail={"code": "TILE_NOT_FOUND", "message": tile_name}
        )

    meta = await run_in_thread(get_dataset_meta, spec.dataset_id)
    plan = await run_in_thread(_zoom_plan, chart_id, meta, spec, zoom, dpr)
    tile_request = TileRequest(
        meta, spec, plan.lattice, TileAddress(zoom, index), dpr, plan.y_ranges
    )
    etag = hashlib.sha256(tile_request.cache_key().encode()).hexdigest()[:24]

    tiles: list[RenderedTile] = []
    if request.headers.get("if-none-match") != f'"{etag}"':
        tiles.append(await default_tile_renderer.render(tile_request))

    def build() -> Response:
        tile = tiles[0]
        return Response(
            content=tile.image,
            media_type="image/png",
            headers={
                "X-Webchart-Exact": "true" if tile.is_exact else "false",
                "X-Webchart-Source": tile.source,
            },
        )

    return _cached_response(request, etag, build)


# (chart_id, data_version, zoom, dpr) → 그 줌의 프레임 계획. y범위 계산(막대 축은 집계 필요)을
# 타일마다 반복하지 않는다.
_ZOOM_PLANS: dict[tuple[str, str, int, int], FramePlan] = {}
_MAX_ZOOM_PLANS = 1024

# 막대 축 y범위를 x 전체로 집계할 때의 줌 상한 (2^10 × 512열). 더 깊은 줌은 이 줌의 범위를 쓴다.
_MAX_PLANNING_ZOOM = 10


def _zoom_plan(chart_id: str, meta: DatasetMeta, spec: ChartSpec, zoom: int, dpr: int) -> FramePlan:
    key = (chart_id, f"{meta.data_version}:{meta.generation}", zoom, dpr)
    plan = _ZOOM_PLANS.get(key)
    if plan is None:
        lattice = lattice_for(meta, spec.x_axis.column, settings.tile_size)
        planning_zoom = min(zoom, _MAX_PLANNING_ZOOM)
        coarse = plan_frame(meta, spec, lattice.domain, planning_zoom, dpr, prefetch_radius=0)
        plan = FramePlan(lattice, [], [], coarse.y_ranges, dpr)
        if len(_ZOOM_PLANS) >= _MAX_ZOOM_PLANS:
            _ZOOM_PLANS.clear()
        _ZOOM_PLANS[key] = plan
    return plan
