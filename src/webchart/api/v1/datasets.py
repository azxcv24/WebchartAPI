"""데이터셋 API: 업로드, 원격 연결, 목록, 스키마, 미리보기, 피라미드, 행 추가, 삭제.

업로드·원격 연결·피라미드 빌드는 202를 바로 돌려주고 백그라운드에서 처리한다.
진행 상태는 데이터셋 메타데이터의 status로 확인한다.
"""

import logging
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, File, Query, UploadFile
from pydantic import BaseModel, Field

from webchart.config import settings
from webchart.core.concurrency import run_in_thread
from webchart.core.errors import WebchartError, webchart_error_to_http
from webchart.models.dataset import (
    DatasetCreateResponse,
    DatasetDetailResponse,
    DatasetPreviewResponse,
    DatasetStatus,
    PyramidBuildRequest,
    PyramidBuildResponse,
    PyramidInfo,
    RemoteSourceSpec,
)
from webchart.services.data import access
from webchart.services.ingest.upload import (
    convert_to_parquet,
    save_upload,
    write_error_meta,
    write_initial_meta,
)

logger = logging.getLogger("webchart.api.datasets")

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("/upload", response_model=DatasetCreateResponse, status_code=202)
async def upload_dataset(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """데이터 파일(CSV, Parquet, JSON, Excel)을 올린다. Parquet 변환은 백그라운드에서 한다."""
    try:
        temp_path = await save_upload(file)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    dataset_id = str(uuid.uuid4())
    write_initial_meta(
        dataset_id=dataset_id,
        name=Path(file.filename or dataset_id).stem,
        source_format=temp_path.suffix.lstrip("."),
        size_bytes=temp_path.stat().st_size,
    )

    background_tasks.add_task(_process_upload, temp_path, dataset_id)

    return DatasetCreateResponse(
        dataset_id=dataset_id,
        status=DatasetStatus.INGESTING,
    )


async def _process_upload(temp_path: Path, dataset_id: str) -> None:
    """백그라운드 작업: 업로드 파일을 Parquet로 변환한다. 실패하면 메타데이터에 에러를 기록한다."""
    try:
        await run_in_thread(convert_to_parquet, temp_path, dataset_id)
        logger.info(f"Dataset {dataset_id} processed successfully")
    except Exception as e:
        logger.error(f"Failed to process dataset {dataset_id}: {e}")
        write_error_meta(dataset_id, str(e))


@router.post("/connect", response_model=DatasetCreateResponse, status_code=202)
async def connect_remote(
    spec: RemoteSourceSpec,
    background_tasks: BackgroundTasks,
):
    """원격 데이터 소스(S3, HTTP, FTP, SFTP, SMB, ClickHouse 등)를 Parquet로 가져온다."""
    from webchart.services.ingest.registry import get_backend

    try:
        backend = get_backend(spec.uri)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    dataset_id = str(uuid.uuid4())
    write_initial_meta(
        dataset_id=dataset_id,
        name=Path(spec.uri).name or dataset_id,
        source_format=spec.protocol,
    )

    async def _ingest() -> None:
        try:
            await backend.ingest_to_parquet(spec, settings.data_dir / "datasets" / dataset_id)
            logger.info(f"Remote dataset {dataset_id} ingested successfully")
        except Exception as e:
            logger.error(f"Failed to ingest remote dataset {dataset_id}: {e}")
            write_error_meta(dataset_id, str(e))

    background_tasks.add_task(_ingest)

    return DatasetCreateResponse(
        dataset_id=dataset_id,
        status=DatasetStatus.INGESTING,
    )


@router.get("", response_model=list[DatasetDetailResponse])
async def list_datasets(
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """데이터셋 목록 (최신순, 페이지 단위)."""
    datasets = access.list_datasets(limit=limit, offset=offset)
    return [DatasetDetailResponse(**d.model_dump()) for d in datasets]


@router.get("/{dataset_id}", response_model=DatasetDetailResponse)
async def get_dataset(dataset_id: str):
    """데이터셋 메타데이터를 조회한다."""
    try:
        meta = access.get_dataset_meta(dataset_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    return DatasetDetailResponse(**meta.model_dump())


@router.get("/{dataset_id}/schema")
async def get_dataset_schema(dataset_id: str):
    """컬럼 스키마와 컬럼별 통계를 조회한다."""
    try:
        meta = access.get_dataset_meta(dataset_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    return {"columns": [c.model_dump() for c in meta.columns]}


@router.get("/{dataset_id}/preview", response_model=DatasetPreviewResponse)
async def preview_dataset(
    dataset_id: str,
    rows: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    """데이터셋 앞쪽 N행을 미리 본다."""
    try:
        meta = access.get_dataset_meta(dataset_id)
        sample = await run_in_thread(access.read_sample, dataset_id, rows)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    return DatasetPreviewResponse(
        columns=list(sample.columns),
        data=sample.values.tolist(),
        total_rows=meta.row_count,
        preview_rows=len(sample),
    )


@router.post(
    "/{dataset_id}/pyramid",
    response_model=PyramidBuildResponse,
    status_code=202,
)
async def build_pyramid_endpoint(
    dataset_id: str,
    req: PyramidBuildRequest,
    background_tasks: BackgroundTasks,
):
    """x 컬럼 하나에 대한 다중 해상도 집계 피라미드를 백그라운드에서 만든다.

    완료 여부는 GET /{dataset_id}/pyramids로 확인한다. 피라미드가 있으면 줌아웃 화면을
    원본 전체를 읽지 않고 미리 집계한 레벨에서 정확하게 그린다.
    """
    from webchart.application.pyramids import build_pyramid

    try:
        access.get_dataset_meta(dataset_id)  # 없으면 404
    except WebchartError as e:
        raise webchart_error_to_http(e)

    async def _build() -> None:
        try:
            await run_in_thread(build_pyramid, dataset_id, req.x_column, req.columns)
            logger.info(f"Pyramid built for {dataset_id}/{req.x_column}")
        except Exception as e:
            logger.error(f"Pyramid build failed for {dataset_id}/{req.x_column}: {e}")

    background_tasks.add_task(_build)

    return PyramidBuildResponse(
        dataset_id=dataset_id,
        x_column=req.x_column,
        status="building",
    )


class AppendRowsRequest(BaseModel):
    """열 형식 행 묶음: `{"columns": {"ts": [...], "price": [...]}}`.

    시간 컬럼 값은 ISO 8601 문자열이다. 빠진 컬럼은 결측으로 채운다.
    """

    columns: dict[str, list[Any]] = Field(min_length=1)


class AppendRowsResponse(BaseModel):
    dataset_id: str
    generation: int
    rows: int
    ranges: dict[str, tuple[float, float]]
    grown_columns: list[str]


@router.post("/{dataset_id}/rows", response_model=AppendRowsResponse)
async def append_rows(dataset_id: str, request: AppendRowsRequest) -> AppendRowsResponse:
    """행을 실시간으로 추가한다 (계획 문서 4.3).

    피라미드는 다시 빌드하지 않고 추가된 행만 merge한다. 그림이 바뀐 x 구간의 타일만
    캐시 키가 바뀌고, 그 데이터셋을 보는 WebSocket 세션에 `data_appended`가 간다.
    """
    from webchart.application.append import append_columns

    try:
        result = await run_in_thread(append_columns, dataset_id, request.columns)
    except WebchartError as e:
        raise webchart_error_to_http(e)
    return AppendRowsResponse(
        dataset_id=result.dataset_id,
        generation=result.generation,
        rows=result.rows,
        ranges=result.ranges,
        grown_columns=result.grown_columns,
    )


@router.get("/{dataset_id}/pyramids", response_model=dict[str, PyramidInfo])
async def list_pyramids(dataset_id: str):
    """이미 만든 피라미드 목록. 키는 x 컬럼 이름이다."""
    try:
        meta = access.get_dataset_meta(dataset_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)
    return meta.pyramids


@router.delete("/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: str):
    """데이터셋과 그것을 참조하는 세션을 지운다. 세션 정리 실패는 경고만 남긴다."""
    from webchart.cache.redis_cache import delete_sessions_for_dataset

    try:
        access.delete_dataset(dataset_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    from webchart.application.tiles import tile_memory_cache

    tile_memory_cache.discard_prefix(f"tile:{dataset_id}:")

    try:
        removed = await delete_sessions_for_dataset(dataset_id)
        if removed:
            logger.info(f"Removed {removed} session(s) referencing {dataset_id}")
    except Exception as e:
        logger.warning(f"Failed to purge sessions for {dataset_id}: {e}")
