from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DatasetStatus(str, Enum):
    UPLOADING = "uploading"
    INGESTING = "ingesting"
    READY = "ready"
    ERROR = "error"


class ColumnInfo(BaseModel):
    name: str
    dtype: str
    nullable: bool = True
    min_value: float | str | None = None
    max_value: float | str | None = None
    null_count: int = 0
    unique_count: int | None = None


class DatasetMeta(BaseModel):
    dataset_id: str
    name: str
    status: DatasetStatus = DatasetStatus.UPLOADING
    row_count: int = 0
    size_bytes: int = 0
    columns: list[ColumnInfo] = Field(default_factory=list)
    partition_count: int = 0
    source_format: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    error_message: str | None = None


class RemoteSourceSpec(BaseModel):
    protocol: str = Field(description="s3, ftp, sftp, http, https, smb, file")
    uri: str = Field(description="Full URI to the data source")
    credentials: dict[str, str] = Field(
        default_factory=dict,
        description="Protocol-specific credentials",
    )
    options: dict[str, str] = Field(
        default_factory=dict,
        description="Additional connection options",
    )


class DatasetCreateResponse(BaseModel):
    dataset_id: str
    status: DatasetStatus


class DatasetDetailResponse(BaseModel):
    dataset_id: str
    name: str
    status: DatasetStatus
    row_count: int
    size_bytes: int
    columns: list[ColumnInfo]
    partition_count: int
    source_format: str
    created_at: datetime
    error_message: str | None = None


class DatasetPreviewResponse(BaseModel):
    columns: list[str]
    data: list[list[object]]
    total_rows: int
    preview_rows: int
