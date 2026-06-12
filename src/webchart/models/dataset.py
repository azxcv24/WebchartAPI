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


class PyramidInfo(BaseModel):
    """Metadata for a multi-resolution pyramid built on one x column."""

    x_column: str
    columns: list[str]
    max_level: int  # levels 0..max_level are available
    points_per_tile: int  # target points each tile wants (drives level pick)
    method: str = "minmax"
    total_points: int = 0  # rows in the finest level
    x_min: float | None = None
    x_max: float | None = None
    built_at: datetime = Field(default_factory=_utcnow)


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
    # Pyramids keyed by x_column name.
    pyramids: dict[str, PyramidInfo] = Field(default_factory=dict)


class RemoteSourceSpec(BaseModel):
    protocol: str = Field(
        description="file, s3, minio, http, https, ftp, sftp, smb, clickhouse, clickhouses"
    )
    uri: str = Field(
        description=(
            "Full URI. Examples: s3://bucket/key.parquet, "
            "https://host/file.csv, ftp://host/path/file.csv, "
            "smb://host/share/file.csv, clickhouse://host:8123/db"
        )
    )
    credentials: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Protocol-specific credentials. s3/minio: access_key, secret_key; "
            "http: bearer or username/password; ftp/sftp/smb: username, password "
            "(sftp also private_key; smb also domain); clickhouse: username, password"
        ),
    )
    options: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Connection options. s3/minio: endpoint_url, region, addressing_style; "
            "http: header_<Name>; sftp: known_hosts; smb: port; "
            "clickhouse: query or table, limit, secure"
        ),
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
