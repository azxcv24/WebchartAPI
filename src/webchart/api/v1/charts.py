"""정적 차트 렌더 API."""

import uuid

from fastapi import APIRouter
from fastapi.responses import Response

from webchart.application.static_chart import RenderedChart, render_static_chart
from webchart.core.concurrency import run_in_thread
from webchart.core.errors import WebchartError, webchart_error_to_http
from webchart.models.chart import ChartSpec, ImageFormat, RenderResult

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
