from pathlib import Path

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

    # Multi-resolution pyramid (pre-aggregated overview levels)
    pyramid_points_per_tile: int = 512  # points each tile targets
    pyramid_max_points: int = 2_000_000  # cap on rows in the finest level
    pyramid_max_level: int = 20  # hard cap on pyramid depth

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
    max_downsample_points: int = 4800  # 2 * default width

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

    # Rate limiting (per API key, or per client IP when auth is off).
    # In-memory limiter scoped to the worker process; for multi-worker setups
    # pair with a sticky-session LB or swap in a Redis-backed limiter.
    rate_limit_per_minute: int = 120
    rate_limit_enabled: bool = True


settings = Settings()
