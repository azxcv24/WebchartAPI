"""서버 설정. 모든 값은 `WEBCHART_` 접두사 환경 변수로 덮어쓴다."""

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "WEBCHART_"}

    # 디버그 로그 (바인드 주소·포트는 uvicorn 실행 인자로 정한다)
    debug: bool = False

    # 타일 캐시(L2)와 세션 상태를 두는 Redis
    redis_url: str = "redis://localhost:6379/0"

    # 데이터셋(Parquet·메타데이터·피라미드) 저장 루트
    data_dir: Path = Path("/data/webchart")
    max_upload_size: int = 10 * 1024 * 1024 * 1024  # 업로드 상한 (바이트, 10GB)
    # xlsx 업로드 상한 (바이트, 200MB). xlsx는 청크로 읽지 못하고 통째로 메모리에 올린다.
    max_xlsx_upload_size: int = 200 * 1024 * 1024
    # 원본 Parquet 로우그룹 크기(행). 범위 읽기의 최소 단위라 작을수록 깊은 줌 읽기가 싸고,
    # 클수록 압축·스캔 효율이 좋다. 2^17 = 131,072행.
    ingest_row_group_rows: int = 131_072

    # 원격 수집 커넥터
    # file:// 인제스트가 읽을 수 있는 디렉터리. None이면 file:// 인제스트를 거부한다
    # (서버 디스크의 임의 파일을 데이터셋으로 읽어 갈 수 없게).
    local_ingest_root: Path | None = None
    connector_timeout: int = 300  # 원격 다운로드·쿼리 한 번의 제한 시간 (초)
    clickhouse_max_rows: int = 10_000_000  # ClickHouse 쿼리 한 번에 가져올 행 수 상한

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

    # HTTP/S3 커넥터의 SSRF 방어. 켜져 있으면 사설·루프백·링크 로컬 주소로 풀리는 호스트를
    # 거부한다. 내부 URL을 일부러 읽는 신뢰된 네트워크(예: 공개 엔드포인트 없는 클러스터 내
    # MinIO)에서만 끈다.
    block_private_egress: bool = True

    # 렌더 스레드 풀 크기. WS 연결 하나가 동시에 렌더하는 타일 수의 상한이기도 하다.
    render_pool_size: int = 8
    # 그리기(matplotlib) 프로세스 수. matplotlib은 GIL에 묶여 스레드로는 늘지 않는다.
    # 0이면 렌더 스레드에서 그린다 (테스트·작은 서버).
    paint_processes: int = Field(default_factory=lambda: min(8, max(1, (os.cpu_count() or 2) // 2)))
    tile_size: int = 512  # 타일 한 변의 CSS 픽셀 수 (2의 거듭제곱)

    # 프로세스 내 L1 타일 캐시 상한 (바이트). Redis(L2) 앞에서 같은 워커의 재요청을 받는다.
    tile_memory_cache_bytes: int = 512 * 1024**2
    # /metrics (Prometheus 텍스트) 노출 여부
    metrics_enabled: bool = True

    # 캐시 TTL (초): 타일 캐시, 마지막 접근 이후 세션 유지 시간
    tile_cache_ttl: int = 3600
    session_ttl: int = 900

    # 업로드를 받는 파일 확장자
    allowed_upload_extensions: list[str] = [".csv", ".parquet", ".json", ".xlsx", ".tsv"]

    # CORS 허용 출처 목록. CORS 규격상 자격 증명을 켜면 "*"를 쓸 수 없으므로 출처를 하나씩
    # 적는다.
    cors_allow_origins: list[str] = ["http://localhost:3000"]
    cors_allow_credentials: bool = False

    # API 키 인증. 빈 목록이면 인증을 끈다 (개발용).
    # 운영에서는 WEBCHART_API_KEYS='["key1", "key2"]'처럼 지정한다.
    api_keys: list[str] = []
    api_key_header: str = "X-API-Key"
    # 임베드 이미지·XYZ 타일 URL 토큰의 HMAC 비밀값. 비우면 api_keys에서 만든다
    # (키를 바꾸면 기존 임베드 URL이 무효가 되므로, 운영에서는 따로 지정하는 것이 좋다).
    embed_secret: str = ""

    # 요청 속도 제한 (분당 횟수, API 키별. 인증이 꺼져 있으면 클라이언트 IP별).
    # 워커 프로세스 메모리에 세므로, 워커가 여럿이면 스티키 세션 LB와 함께 쓰거나
    # Redis 기반 limiter로 바꿔야 한다.
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
