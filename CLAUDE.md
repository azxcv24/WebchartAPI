# CLAUDE.md - WebchartAPI

## Project Overview

WebchartAPI는 대용량 데이터(수백 GB)를 서버사이드에서 차트로 렌더링하여 제공하는 Python FastAPI 기반 API 서비스.

## Tech Stack

- **Language**: Python 3.11+
- **Framework**: FastAPI (async)
- **Data Processing**: Dask (out-of-core), Apache Arrow (in-memory), Polars
- **Chart Rendering**: Matplotlib (static/tile), Plotly (interactive export)
- **Real-time**: WebSocket (starlette)
- **Cache**: Redis
- **Data Format**: Apache Parquet (canonical storage)
- **Container**: Docker + docker-compose

## Project Structure

```
src/webchart/           # Main source package
├── main.py             # FastAPI app factory, lifespan events
├── config.py           # Pydantic Settings (env-driven)
├── dependencies.py     # FastAPI DI providers
├── api/v1/             # HTTP + WebSocket endpoints
├── models/             # Pydantic request/response schemas
├── services/
│   ├── ingest/         # Data ingestion (upload, S3, FTP, SFTP, HTTP, SMB, local)
│   ├── data/           # Data access, downsampling (LTTB, min-max), transforms
│   ├── render/         # Chart rendering (static, tile-based, overlay)
│   └── session/        # Session lifecycle, interaction, WebSocket streaming
├── cache/              # Redis cache layer
└── core/               # Errors, logging, concurrency utils
tests/                  # pytest tests (mirrors src structure)
```

## Key Architecture Concepts

### Tile-based Rendering
동적 차트는 웹 지도 타일 서버처럼 데이터 범위를 격자로 분할하여 뷰포트에 보이는 타일만 렌더링. 각 줌 레벨에서 2^N개의 수평 타일로 분할.

### Downsampling
대용량 데이터를 화면 해상도에 맞게 축소. LTTB(라인차트), Min-Max(캔들스틱), Average(바차트) 알고리즘 사용. 타겟 포인트 수 = 2 * 픽셀 너비.

### WebSocket Protocol
`/api/v1/ws/{session_id}`로 연결. JSON 메시지(viewport_change, hover, scroll 등)와 바이너리 프레임(타일 이미지)을 혼합 사용.

### Data Flow
1. 데이터 수집 → Parquet 변환 (ingest)
2. Dask로 필요한 파티션만 로드 (data/access)
3. 다운샘플링 (data/downsample)
4. 렌더링 (render/)
5. Redis 캐싱 (cache/)

## Common Commands

```bash
# Run dev server
uvicorn src.webchart.main:app --reload --host 0.0.0.0 --port 8000

# Run tests
pytest tests/ -v

# Run with docker
docker-compose up -d

# Type checking
mypy src/webchart/

# Linting
ruff check src/
ruff format src/
```

## v2 개선 진행 중 — 반드시 먼저 읽을 것

`docs/improvement-plan.md`가 현재 개발 방향의 기준 문서다 (무왜곡 픽셀 정렬 집계, Phase 0~4).
- 핵심 목표: 대용량 데이터를 **왜곡 없이** 서버에서 그리고, 브라우저는 DOM 최소(차트당 ≤ 3개), 인터랙션은 WS 스트리밍
- 무왜곡 = 오라클 테스트(원본 전체 렌더와 픽셀 비교)로 검증되는 계약. 근사 경로는 응답에 `exact=false` 표기
- 아래 "Downsampling"의 LTTB 기본값은 Phase 1에서 M4(픽셀 열별 first/last/min/max)로 교체 예정
- 코드 컨벤션(이름에 단위, `[start, end)` 범위, 한국어 docstring, 패턴 적용 기준)은 계획 문서 6장을 따른다

## Conventions

- 모든 API 엔드포인트는 `/api/v1/` 접두사 사용
- Pydantic v2 모델 사용 (request/response schemas)
- 비동기 I/O 우선, CPU-bound 작업은 ThreadPoolExecutor 사용
- 데이터 수집 커넥터는 `IngestBackend` 프로토콜 구현
- 차트 렌더러는 `ChartRenderer` 프로토콜 구현
- 캐시 키 형식: `{type}:{dataset_id}:{hash}` (예: `tile:abc123:z3-x4-y2`)
- 에러 응답은 `core/errors.py`의 커스텀 예외 계층 사용
- 환경 변수 기반 설정 (Pydantic Settings)

## Environment Variables

```
WEBCHART_REDIS_URL=redis://localhost:6379/0
WEBCHART_DATA_DIR=/data/webchart          # Parquet 저장 경로
WEBCHART_MAX_UPLOAD_SIZE=10737418240      # 10GB
WEBCHART_RENDER_POOL_SIZE=8               # 동시 렌더링 스레드 수
WEBCHART_TILE_SIZE=512                    # 타일 픽셀 크기
WEBCHART_TILE_CACHE_TTL=3600              # 타일 캐시 TTL (초)
WEBCHART_SESSION_TTL=900                  # 세션 TTL (초)
```