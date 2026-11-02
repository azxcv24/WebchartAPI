from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DatasetStatus(str, Enum):
    UPLOADING = "uploading"
    INGESTING = "ingesting"
    READY = "ready"
    ERROR = "error"


class ColumnKind(str, Enum):
    """컬럼을 x축으로 쓸 수 있는지 판단하는 분류."""

    NUMERIC = "numeric"  # 정수·실수·decimal
    TIME = "time"  # timestamp·date
    OTHER = "other"  # 문자열·불리언 등. x축으로 쓸 수 없다


# kind 필드가 없던 이전 meta.json의 dtype 문자열을 분류할 때 쓴다.
_LEGACY_NUMERIC_DTYPES = {
    "int8", "int16", "int32", "int64",
    "uint8", "uint16", "uint32", "uint64",
    "float16", "float32", "float64", "double",
}
_LEGACY_TIME_DTYPES = {"datetime", "date"}


class ColumnInfo(BaseModel):
    """컬럼 스키마와 통계.

    최솟값·최댓값은 두 가지로 보관한다.
    - `min_value`/`max_value`: 사람이 읽는 표시값. 수치는 수치, 시간은 ISO 8601 문자열
    - `min_numeric`/`max_numeric`: x축 계산에 쓰는 정규 좌표 (services/data/x_domain.py).
      수치는 값 그대로, 시간은 epoch 나노초. x축으로 쓸 수 없는 컬럼은 None
    """

    name: str
    dtype: str
    kind: ColumnKind = ColumnKind.OTHER
    nullable: bool = True
    min_value: float | str | None = None
    max_value: float | str | None = None
    min_numeric: float | None = None
    max_numeric: float | None = None
    # tz-aware 시간 컬럼의 원본 시간대 (예: "UTC", "+09:00"). naive 시간은 None
    timezone: str | None = None
    null_count: int = 0
    unique_count: int | None = None

    @model_validator(mode="after")
    def _backfill_legacy_meta(self) -> "ColumnInfo":
        """kind 필드가 없던 이전 meta.json을 읽을 때 dtype으로 분류를 채운다.

        이전 시간 컬럼은 정규 좌표를 복원할 수 없어 None으로 남는다 (재인제스트 필요).
        """
        if "kind" in self.model_fields_set:
            return self
        if self.dtype in _LEGACY_NUMERIC_DTYPES:
            self.kind = ColumnKind.NUMERIC
            if isinstance(self.min_value, int | float) and isinstance(self.max_value, int | float):
                self.min_numeric = float(self.min_value)
                self.max_numeric = float(self.max_value)
        elif self.dtype in _LEGACY_TIME_DTYPES:
            self.kind = ColumnKind.TIME
        return self


# 현재 피라미드 형식. 1 = 이전 min-max 행 선택 형식(더 이상 쓰지 않음), 2 = BucketStats 격자
PYRAMID_FORMAT_VERSION = 2


class PyramidInfo(BaseModel):
    """x 컬럼 하나에 대한 다중 해상도 집계 피라미드 (계획 문서 2.2~2.4).

    레벨 L은 x 전체 범위 `[domain_start, domain_end)`를 2^L개 버킷으로 균등 분할하고,
    버킷마다 모든 시리즈의 BucketStats를 담는다. 타일 픽셀 열 하나가 버킷 하나와 정확히
    겹치므로(L = z + log2(타일 px · dpr)) 피라미드에서 그린 타일도 원본과 같은 그림이다.

    이전 형식(version 1)의 meta.json도 읽을 수 있지만 사용하지 않는다 (다시 빌드해야 한다).
    """

    version: int = 1
    x_column: str
    columns: list[str]
    max_level: int  # 레벨 0..max_level을 쓸 수 있다
    method: str = "bucket_stats"
    # 빌드한 원본의 data_version. 재인제스트로 달라지면 이 피라미드는 쓰지 않는다.
    data_version: str = ""
    domain_start: float | None = None
    domain_end: float | None = None
    size_bytes: int = 0
    built_at: datetime = Field(default_factory=_utcnow)


class DatasetMeta(BaseModel):
    """데이터셋 메타데이터 (datasets/{id}/meta.json).

    - `data_version`: 인제스트할 때마다 새로 발급하는 식별자. 모든 캐시 키와 피라미드에
      들어가므로, 같은 ID로 다시 인제스트하면 이전 캐시·피라미드가 자연히 무효가 된다.
      이전 형식의 meta.json은 빈 문자열이다.
    - `sort_column`: 원본이 이 컬럼 오름차순으로 저장되어 있다 (계획 문서 2.7).
      None이면 정렬되지 않은 원본이다 (범위 읽기가 느리지만 결과는 같다).
    """

    dataset_id: str
    name: str
    status: DatasetStatus = DatasetStatus.UPLOADING
    data_version: str = ""
    sort_column: str | None = None
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


class PyramidBuildRequest(BaseModel):
    x_column: str = Field(description="피라미드의 x축 컬럼 (수치 또는 시간)")
    columns: list[str] | None = Field(
        default=None,
        description="집계할 y 컬럼 (기본값: x를 뺀 모든 수치 컬럼)",
    )


class PyramidBuildResponse(BaseModel):
    dataset_id: str
    x_column: str
    status: str  # "building"
