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

    # Query guards
    max_query_rows: int = 5_000_000  # cap on read_window result size

    # Rendering
    render_pool_size: int = 8
    tile_size: int = 512
    default_chart_width: int = 1200
    default_chart_height: int = 600
    max_downsample_points: int = 4800  # 2 * default width

    # Cache TTL (seconds)
    tile_cache_ttl: int = 3600
    query_cache_ttl: int = 1800
    session_ttl: int = 900

    # Supported formats
    allowed_upload_extensions: list[str] = [".csv", ".parquet", ".json", ".xlsx", ".tsv"]


settings = Settings()
