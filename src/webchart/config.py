from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "WEBCHART_"}

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Data storage
    data_dir: Path = Path("/data/webchart")
    max_upload_size: int = 10 * 1024 * 1024 * 1024  # 10GB
    max_xlsx_upload_size: int = 200 * 1024 * 1024  # 200MB - xlsx is not chunked
    parquet_partition_size: str = "128MB"
    # 원본 Parquet 로우그룹 크기(행). 범위 읽기의 최소 단위라 작을수록 깊은 줌 읽기가 싸고,
    # 클수록 압축·스캔 효율이 좋다. 2^17 = 131,072행.
    ingest_row_group_rows: int = 131_072

    # Query guards
    max_query_rows: int = 5_000_000  # cap on read_window result size

    # Remote ingest connectors
    connector_timeout: int = 300  # seconds, per remote download/query
    clickhouse_max_rows: int = 10_000_000  # cap on rows pulled from a query

    # 다중 해상도 피라미드 (계획 문서 2.2, 2.8)
    pyramid_max_level: int = 24  # 레벨 상한. 2^24 ≈ 1,670만 버킷
    # 피라미드 하나(모든 레벨 합)의 디스크·메모리 상한. 최대 레벨을 이 예산으로 정한다.
    pyramid_max_bytes: int = 4 * 1024**3
    # 2D 밀도 피라미드 (scatter, heatmap): x 레벨 상한과 y 행 수.
    # 행 수는 타일 높이(tile_size × dpr 최대 4)의 배수여야 모든 dpr에서 정확하다.
    density_max_level: int = 12
    density_rows: int = 2048
    # 정적 렌더에서 원본을 직접 집계(정확)할지 판단하는 행 수 상한. 넘으면 피라미드 근사를 쓴다.
    exact_raw_row_budget: int = 20_000_000

    # SSRF protection for HTTP/S3 connectors. When True (default), connectors
    # refuse hosts that resolve to private/loopback/link-local addresses.
    # Set False only in trusted networks where fetching internal URLs is
    # intended (e.g. an in-cluster MinIO without a public endpoint).
    block_private_egress: bool = True

    # Rendering
    render_pool_size: int = 8
    tile_size: int = 512
    default_chart_width: int = 1200
    default_chart_height: int = 600

    # Cache TTL (seconds)
    tile_cache_ttl: int = 3600
    session_ttl: int = 900

    # Supported formats
    allowed_upload_extensions: list[str] = [".csv", ".parquet", ".json", ".xlsx", ".tsv"]

    # CORS — explicit allow-list. When credentials are enabled, "*" is invalid
    # per the CORS spec, so callers must list concrete origins.
    cors_allow_origins: list[str] = ["http://localhost:3000"]
    cors_allow_credentials: bool = False

    # API key authentication. Empty list disables auth (dev only).
    # Set WEBCHART_API_KEYS='["key1", "key2"]' in production.
    api_keys: list[str] = []
    api_key_header: str = "X-API-Key"
    # 임베드 이미지·XYZ 타일 URL 토큰의 HMAC 비밀값. 비우면 api_keys에서 만든다
    # (키를 바꾸면 기존 임베드 URL이 무효가 되므로, 운영에서는 따로 지정하는 것이 좋다).
    embed_secret: str = ""

    # Rate limiting (per API key, or per client IP when auth is off).
    # In-memory limiter scoped to the worker process; for multi-worker setups
    # pair with a sticky-session LB or swap in a Redis-backed limiter.
    rate_limit_per_minute: int = 120
    rate_limit_enabled: bool = True

    @field_validator("tile_size")
    @classmethod
    def _tile_size_is_power_of_two(cls, value: int) -> int:
        # 타일 픽셀 열 = 피라미드 버킷이 되려면 2의 거듭제곱이어야 한다 (계획 문서 2.2)
        if value <= 0 or value & (value - 1):
            raise ValueError(f"tile_size는 2의 거듭제곱이어야 한다: {value}")
        return value


settings = Settings()
