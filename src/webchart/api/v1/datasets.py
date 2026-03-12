"""Dataset API endpoints: upload, connect, list, schema, preview, delete."""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, UploadFile, File, Query

from webchart.core.concurrency import run_in_thread
from webchart.core.errors import WebchartError, webchart_error_to_http
from webchart.models.dataset import (
    DatasetCreateResponse,
    DatasetDetailResponse,
    DatasetMeta,
    DatasetPreviewResponse,
    DatasetStatus,
    RemoteSourceSpec,
)
from webchart.services.data import access
from webchart.services.ingest.upload import convert_to_parquet, save_upload

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.post("/upload", response_model=DatasetCreateResponse, status_code=202)
async def upload_dataset(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """Upload a data file (CSV, Parquet, JSON, Excel)."""
    try:
        temp_path = await save_upload(file)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    dataset_id = str(uuid.uuid4())

    # Process in background
    background_tasks.add_task(
        _process_upload,
        temp_path,
        dataset_id,
    )

    return DatasetCreateResponse(
        dataset_id=dataset_id,
        status=DatasetStatus.INGESTING,
    )


async def _process_upload(temp_path, dataset_id: str) -> None:
    """Background task: convert uploaded file to Parquet."""
    import logging
    logger = logging.getLogger("webchart.api.datasets")
    try:
        await run_in_thread(convert_to_parquet, temp_path, dataset_id)
        logger.info(f"Dataset {dataset_id} processed successfully")
    except Exception as e:
        logger.error(f"Failed to process dataset {dataset_id}: {e}")


@router.post("/connect", response_model=DatasetCreateResponse, status_code=202)
async def connect_remote(
    spec: RemoteSourceSpec,
    background_tasks: BackgroundTasks,
):
    """Connect to a remote data source."""
    from webchart.services.ingest.registry import get_backend

    try:
        backend = get_backend(spec.uri)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    dataset_id = str(uuid.uuid4())

    async def _ingest():
        import logging
        logger = logging.getLogger("webchart.api.datasets")
        try:
            from webchart.config import settings
            await backend.ingest_to_parquet(spec, settings.data_dir / "datasets" / dataset_id)
            logger.info(f"Remote dataset {dataset_id} ingested successfully")
        except Exception as e:
            logger.error(f"Failed to ingest remote dataset {dataset_id}: {e}")

    background_tasks.add_task(_ingest)

    return DatasetCreateResponse(
        dataset_id=dataset_id,
        status=DatasetStatus.INGESTING,
    )


@router.get("", response_model=list[DatasetDetailResponse])
async def list_datasets():
    """List all datasets."""
    datasets = access.list_datasets()
    return [
        DatasetDetailResponse(**d.model_dump())
        for d in datasets
    ]


@router.get("/{dataset_id}", response_model=DatasetDetailResponse)
async def get_dataset(dataset_id: str):
    """Get dataset metadata."""
    try:
        meta = access.get_dataset_meta(dataset_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)

    return DatasetDetailResponse(**meta.model_dump())


@router.get("/{dataset_id}/schema")
async def get_dataset_schema(dataset_id: str):
    """Get dataset column schema with statistics."""
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
    """Preview first N rows of a dataset."""
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


@router.delete("/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: str):
    """Delete a dataset."""
    try:
        access.delete_dataset(dataset_id)
    except WebchartError as e:
        raise webchart_error_to_http(e)
