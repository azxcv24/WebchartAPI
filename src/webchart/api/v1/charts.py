"""Chart rendering API endpoints."""

import uuid

from fastapi import APIRouter
from fastapi.responses import Response

from webchart.core.concurrency import run_in_thread
from webchart.core.errors import WebchartError, webchart_error_to_http
from webchart.models.chart import ChartSpec, ImageFormat, RenderResult
from webchart.services.data.access import read_window
from webchart.services.data.downsample import downsample
from webchart.services.render.matplotlib_renderer import MatplotlibRenderer

router = APIRouter(prefix="/charts", tags=["charts"])

_renderer = MatplotlibRenderer()

_MIME_TYPES = {
    ImageFormat.PNG: "image/png",
    ImageFormat.SVG: "image/svg+xml",
    ImageFormat.WEBP: "image/webp",
}


@router.post("/render")
async def render_chart(spec: ChartSpec):
    """Render a static chart image."""
    try:
        # Read data
        x_col = spec.x_axis.column
        y_columns = _get_y_columns(spec)
        columns = [x_col] + y_columns

        x_range = spec.x_axis.range
        data = await run_in_thread(
            read_window, spec.dataset_id, x_col, x_range, columns,
        )

        # Downsample if necessary
        target_points = spec.width * 2
        if len(data) > target_points and y_columns:
            data = downsample(
                data, x_col, y_columns[0], target_points, spec.downsample_algorithm,
            )

        # Render
        image_bytes = await run_in_thread(_renderer.render, spec, data)

    except WebchartError as e:
        raise webchart_error_to_http(e)

    return Response(
        content=image_bytes,
        media_type=_MIME_TYPES[spec.format],
        headers={
            "X-Render-Id": str(uuid.uuid4()),
            "X-Data-Points": str(len(data)),
        },
    )


@router.post("/render/info", response_model=RenderResult)
async def render_chart_info(spec: ChartSpec):
    """Render chart and return metadata (without the image)."""
    try:
        x_col = spec.x_axis.column
        y_columns = _get_y_columns(spec)
        columns = [x_col] + y_columns

        data = await run_in_thread(
            read_window, spec.dataset_id, x_col, spec.x_axis.range, columns,
        )

        target_points = spec.width * 2
        if len(data) > target_points and y_columns:
            data = downsample(
                data, x_col, y_columns[0], target_points, spec.downsample_algorithm,
            )

        image_bytes = await run_in_thread(_renderer.render, spec, data)

    except WebchartError as e:
        raise webchart_error_to_http(e)

    return RenderResult(
        render_id=str(uuid.uuid4()),
        format=spec.format,
        width=spec.width,
        height=spec.height,
        size_bytes=len(image_bytes),
    )


def _get_y_columns(spec: ChartSpec) -> list[str]:
    if spec.series:
        return [s.column for s in spec.series]
    elif spec.y_columns:
        return spec.y_columns
    elif spec.y_axis:
        return [spec.y_axis.column]
    return []
