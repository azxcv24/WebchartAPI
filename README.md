# WebchartAPI

대용량 데이터(수백 GB)를 서버사이드에서 렌더링하여 차트로 시각화하는 API 서비스.

## Overview

WebchartAPI는 클라이언트가 감당할 수 없는 대규모 데이터셋을 서버에서 처리하고 차트를 렌더링하여 제공합니다. 정적 이미지 차트와 실시간 동적 차트를 모두 지원하며, 동적 차트는 타일 기반 렌더링 + WebSocket 스트리밍으로 클라이언트 부담을 최소화합니다.

### 왜 서버사이드 렌더링인가?

수백 GB 규모의 데이터를 클라이언트에서 직접 처리하는 것은 불가능합니다. WebchartAPI는 대용량 RAM을 가진 서버에서 데이터를 파티션 단위로 읽고, 화면 해상도에 맞게 다운샘플링한 후, 차트 이미지를 렌더링하여 전송합니다. 클라이언트는 이미지를 표시하고 마우스 이벤트만 보내면 되므로 브라우저/모바일에서도 수억 건의 데이터를 탐색할 수 있습니다.

## Architecture

```
┌──────────────┐     HTTP/WebSocket     ┌──────────────────────┐
│    Client     │◄─────────────────────►│   FastAPI Gateway     │
└──────────────┘                        └──────┬───────────────┘
                                               │
                         ┌─────────────────────┼─────────────────────┐
                         │                     │                     │
                  ┌──────▼──────┐    ┌─────────▼────────┐   ┌───────▼───────┐
                  │ Data Ingest │    │  Chart Render     │   │ Session &     │
                  │ Service     │    │  Engine           │   │ Interaction   │
                  │             │    │  - Static (PNG)   │   │ Manager       │
                  │ Upload/     │    │  - Tile-based     │   │ - Hover/Click │
                  │ Remote Pull │    │  - Overlay        │   │ - Pan/Zoom    │
                  └──────┬──────┘    └─────────┬────────┘   │ - Scroll      │
                         │                     │            └───────┬───────┘
                  ┌──────▼─────────────────────▼────────────────────▼───────┐
                  │              Data Access Layer (Dask + Arrow)           │
                  └──────┬─────────────────────────────────┬───────────────┘
                         │                                 │
                  ┌──────▼──────┐                   ┌──────▼──────┐
                  │ Data Store  │                   │ Redis Cache │
                  │ (Parquet)   │                   │ - Tiles     │
                  │             │                   │ - Queries   │
                  │ S3/FTP/SFTP │                   │ - Sessions  │
                  │ HTTP/SMB    │                   └─────────────┘
                  │ Local FS    │
                  └─────────────┘
```

### 핵심 설계 원칙

- **Apache Arrow를 표준 인메모리 포맷으로 사용** — 모든 데이터 소스는 수집 시 Arrow 테이블로 변환되어 제로카피 읽기, 컬럼 단위 접근이 가능
- **Dask로 아웃오브코어 연산** — 수백 GB 데이터를 RAM에 전부 올리지 않고 파티션 단위로 지연 처리
- **타일 기반 렌더링** — 웹 지도 타일 서버처럼 뷰포트를 격자로 분할하여 보이는 영역만 렌더링
- **WebSocket 양방향 스트리밍** — Hover 데이터, 스크롤 이벤트, 타일 전송을 단일 WebSocket으로 처리
- **Redis 캐싱** — 렌더링된 타일, 다운샘플 결과, 세션 상태를 TTL 기반으로 캐싱

---

## Key Features

### Data Ingestion
- **파일 업로드**: CSV, Parquet, JSON, Excel, TSV 직접 업로드
- **원격 데이터 접근**: S3, FTP/SFTP, HTTP/HTTPS, SMB/CIFS, 로컬 파일시스템 (프로토콜별 플러그인 구조)
- 업로드된 모든 데이터는 자동으로 파티셔닝된 Parquet으로 변환
- Parquet 메타데이터 기반 자동 스키마 감지 (컬럼 타입, min/max, null count)

### Static Chart Rendering
- 서버에서 완전한 차트 이미지를 렌더링하여 PNG/SVG/WebP로 제공
- 지원 차트 유형: Line, Bar, Scatter, Heatmap, Candlestick
- LTTB 다운샘플링으로 수억 건 데이터도 빠르게 렌더링 (타겟 포인트 수 = 화면 너비 x 2)

### Dynamic Interactive Charts
- **타일 기반 렌더링**: 줌 레벨에 따라 데이터 범위를 2^N개 타일로 분할, 각 타일은 512x512 PNG
- **WebSocket 스트리밍**: 스크롤/줌 시 필요한 타일만 바이너리 프레임으로 실시간 전송
- **Hover/Click 데이터 조회**: 픽셀 좌표를 데이터 좌표로 변환 후 가장 가까운 데이터 포인트 반환
- **프리페치**: 뷰포트 좌우 2타일을 미리 렌더링하여 스크롤 시 즉각 표시
- **오버레이**: 크로스헤어, 툴팁 등을 투명 PNG로 렌더링하여 타일 위에 합성

### Performance
- **Dask**: 수백 GB 데이터셋을 ~128MB 파티션으로 분할하여 필요한 부분만 읽기
- **Apache Arrow**: Parquet 파일에서 제로카피 컬럼 접근
- **Redis 3단 캐시**: 타일 캐시(TTL 1h), 쿼리 캐시(TTL 30m), 세션 캐시(TTL 15m)
- **렌더 풀**: ThreadPoolExecutor로 Matplotlib 렌더링 병렬 처리 (기본 8 워커)
- **공유 타일 캐시**: 같은 데이터셋 + 같은 차트 설정을 보는 여러 사용자가 캐시를 공유

---

## Tech Stack

| Component | Technology |
|---|---|
| API Framework | FastAPI (async) |
| Data Processing | Dask, Apache Arrow, Pandas |
| Chart Rendering | Matplotlib (Agg backend), Plotly |
| Real-time Communication | WebSocket (Starlette) |
| Caching | Redis (hiredis) |
| Data Format | Apache Parquet |
| Containerization | Docker + docker-compose |
| Testing | pytest, pytest-asyncio |
| Linting | Ruff, mypy |

---

## Project Structure

```
WebchartAPI/
├── pyproject.toml               # 프로젝트 메타데이터, 의존성 관리 (Hatchling)
├── Dockerfile                   # 프로덕션 컨테이너 빌드
├── docker-compose.yml           # API + Redis 서비스 구성
├── CLAUDE.md                    # AI 어시스턴트용 프로젝트 컨텍스트
│
├── src/webchart/                # ── 메인 소스 패키지 ──
│   ├── main.py                  # FastAPI 앱 팩토리, lifespan 이벤트 관리
│   ├── config.py                # 환경변수 기반 설정 (Pydantic Settings)
│   ├── dependencies.py          # FastAPI 의존성 주입 프로바이더
│   │
│   ├── api/                     # ── HTTP & WebSocket 엔드포인트 레이어 ──
│   │   ├── router.py            # 전체 라우터 집약 (/api/v1 접두사)
│   │   ├── middleware.py        # RequestId, Timing 미들웨어
│   │   └── v1/
│   │       ├── datasets.py      # 데이터셋 CRUD (업로드, 원격연결, 조회, 삭제)
│   │       ├── charts.py        # 정적 차트 렌더링 엔드포인트
│   │       ├── sessions.py      # 인터랙티브 세션 생성/조회/종료
│   │       └── ws.py            # WebSocket 엔드포인트 (/ws/{session_id})
│   │
│   ├── models/                  # ── Pydantic v2 요청/응답 스키마 ──
│   │   ├── dataset.py           # DatasetMeta, RemoteSourceSpec, ColumnInfo 등
│   │   ├── chart.py             # ChartSpec, ChartType, AxisConfig, SeriesConfig
│   │   ├── session.py           # SessionState, ViewportWindow
│   │   └── ws_messages.py       # WS 프로토콜 메시지 (Hover, Tile, Overlay 등)
│   │
│   ├── services/                # ── 비즈니스 로직 레이어 ──
│   │   │
│   │   ├── ingest/              # 데이터 수집 서비스
│   │   │   ├── base.py          # IngestBackend 프로토콜 (인터페이스 정의)
│   │   │   ├── upload.py        # 파일 업로드 처리 + Parquet 변환
│   │   │   ├── local.py         # 로컬 파일시스템 커넥터 (file:// 프로토콜)
│   │   │   ├── registry.py      # URI 스킴 → 백엔드 디스패처
│   │   │   └── schema_detect.py # Parquet 메타데이터 기반 스키마 자동 감지
│   │   │
│   │   ├── data/                # 데이터 접근 서비스
│   │   │   ├── access.py        # Dask 기반 데이터 읽기 (윈도우, 샘플, 목록)
│   │   │   ├── downsample.py    # 다운샘플링 알고리즘 (LTTB, MinMax, Average)
│   │   │   └── transform.py     # 집계, 히스토그램 빈 계산
│   │   │
│   │   ├── render/              # 차트 렌더링 서비스
│   │   │   ├── base.py          # ChartRenderer 프로토콜 (인터페이스 정의)
│   │   │   ├── matplotlib_renderer.py  # Matplotlib 기반 정적/타일 렌더러
│   │   │   ├── tile_renderer.py # 타일 격자 계산, 캐시 연동, 프리페치
│   │   │   ├── overlay.py       # 크로스헤어 등 투명 오버레이 생성
│   │   │   └── chart_types/     # (확장용) 차트 유형별 드로잉 로직
│   │   │
│   │   └── session/             # 세션 관리 서비스
│   │       ├── manager.py       # 세션 생성/조회/뷰포트 업데이트/종료
│   │       ├── interaction.py   # Hover 좌표 해석, 최근접 데이터 포인트 탐색
│   │       └── stream.py        # WebSocket 메시지 디스패치, 타일 스트리밍
│   │
│   ├── cache/                   # ── Redis 캐시 레이어 ──
│   │   ├── redis_cache.py       # 타일/쿼리/세션 캐시 CRUD, 커넥션 풀 관리
│   │   └── keys.py              # 캐시 키 생성 유틸리티
│   │
│   └── core/                    # ── 공통 유틸리티 ──
│       ├── errors.py            # 에러 계층 (WebchartError → HTTPException 변환)
│       ├── logging.py           # 구조화 로깅 설정
│       ├── concurrency.py       # ThreadPoolExecutor 렌더 풀 관리
│       └── math_utils.py        # 픽셀↔데이터 좌표 변환, 타일 인덱스 계산
│
└── tests/                       # ── 테스트 ──
    ├── conftest.py              # 공유 픽스처 (임시 데이터 디렉토리, 테스트 클라이언트)
    ├── test_api/
    │   └── test_health.py       # 헬스체크 엔드포인트 테스트
    └── test_services/
        ├── test_downsample.py   # LTTB, MinMax, Average 다운샘플링 테스트
        └── test_math_utils.py   # 좌표 변환, 타일 인덱스 계산 테스트
```

---

## Module Details

### `main.py` — 앱 팩토리

FastAPI 애플리케이션을 생성하고 lifespan 이벤트로 시작/종료 시 리소스를 관리합니다.
- 시작: 데이터 디렉토리 생성, 로깅 설정, Ingest 백엔드 등록
- 종료: 렌더 스레드풀 종료, Redis 커넥션 종료
- CORS 미들웨어, RequestId/Timing 미들웨어 적용

### `config.py` — 환경변수 설정

`WEBCHART_` 접두사를 가진 환경변수로 모든 설정을 제어합니다.

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `WEBCHART_REDIS_URL` | `redis://localhost:6379/0` | Redis 연결 URL |
| `WEBCHART_DATA_DIR` | `/data/webchart` | Parquet 데이터 저장 경로 |
| `WEBCHART_MAX_UPLOAD_SIZE` | `10737418240` (10GB) | 최대 업로드 크기 |
| `WEBCHART_RENDER_POOL_SIZE` | `8` | 동시 렌더링 스레드 수 |
| `WEBCHART_TILE_SIZE` | `512` | 타일 픽셀 크기 |
| `WEBCHART_TILE_CACHE_TTL` | `3600` | 타일 캐시 TTL (초) |
| `WEBCHART_QUERY_CACHE_TTL` | `1800` | 쿼리 캐시 TTL (초) |
| `WEBCHART_SESSION_TTL` | `900` | 세션 TTL (초) |

### `services/ingest/` — 데이터 수집

```
[사용자 업로드/원격 URI]
        │
        ▼
  IngestBackend.ingest_to_parquet()
        │
        ▼
  [청크 단위 읽기 → Dask DataFrame]
        │
        ▼
  [~128MB 파티션으로 분할 → Parquet 저장]
        │
        ▼
  [schema_detect → 컬럼 타입/통계 추출 → meta.json 저장]
```

- `base.py`: `IngestBackend` 프로토콜 — `validate_source()`, `ingest_to_parquet()`, `supported_protocols` 인터페이스 정의
- `upload.py`: 업로드 파일을 64MB 청크로 읽어 임시 저장 후, Dask로 Parquet 변환. CSV/TSV/JSON/Excel/Parquet 지원
- `local.py`: `file://` 프로토콜 처리. 로컬 파일을 복사 후 Parquet 변환
- `registry.py`: URI 스킴을 파싱하여 적절한 `IngestBackend` 구현체로 라우팅
- `schema_detect.py`: Parquet 파일의 row group 메타데이터에서 데이터를 로드하지 않고 컬럼 정보(타입, min/max, null count) 추출

### `services/data/` — 데이터 접근 & 다운샘플링

- `access.py`: Dask를 사용한 데이터 읽기 인터페이스
  - `read_window()` — x축 범위 필터링으로 뷰포트에 해당하는 데이터만 로드
  - `read_sample()` — 프리뷰용 랜덤 샘플 추출
  - `list_datasets()` / `delete_dataset()` — 데이터셋 CRUD

- `downsample.py`: 대용량 데이터를 화면 해상도에 맞게 축소하는 3가지 알고리즘
  - **LTTB** (Largest Triangle Three Buckets): 라인 차트에 최적. 시각적 형태를 가장 잘 보존. 첫/마지막 포인트를 항상 유지하고 각 버킷에서 삼각형 넓이가 최대인 포인트를 선택
  - **MinMax**: 캔들스틱/범위 차트에 적합. 각 버킷의 최소/최대값 포인트를 유지하여 데이터 범위를 정확히 표현
  - **Average**: 바 차트/집계 뷰에 적합. 각 버킷의 평균값을 계산

- `transform.py`: 그룹별 집계, 히스토그램 빈 계산

### `services/render/` — 차트 렌더링

- `matplotlib_renderer.py`: Matplotlib Agg 백엔드 기반 렌더러
  - `render()` — 전체 차트 이미지 생성 (제목, 축, 범례 포함)
  - `render_tile()` — 축/범례 없이 데이터만 그린 타일 이미지 생성
  - Line, Bar, Scatter, Heatmap, Candlestick 차트 유형별 드로잉 로직

- `tile_renderer.py`: 타일 기반 동적 차트의 핵심 모듈
  - 데이터 범위를 줌 레벨에 따라 2^N개 타일로 분할
  - 타일별로 해당 범위의 데이터만 읽고 → 다운샘플링 → Matplotlib 렌더 → Redis 캐싱
  - `render_visible_tiles()`: 뷰포트 + 좌우 프리페치 타일까지 한번에 렌더

- `overlay.py`: 크로스헤어 등 인터랙션 표시용 투명 PNG 오버레이 생성

### `services/session/` — 세션 & 인터랙션

- `manager.py`: 세션 생명주기 관리. Redis에 세션 상태 저장, TTL 기반 자동 만료
- `interaction.py`: Hover 이벤트 처리. 픽셀 좌표 → 데이터 좌표 변환 → 최근접 데이터 포인트 탐색
- `stream.py`: WebSocket 메시지 디스패처
  - `viewport_change`: 타일 렌더 + 바이너리 프레임 전송
  - `hover`: 데이터 포인트 조회 + JSON 응답
  - `scroll`: 뷰포트 이동 + 새 엣지 타일 전송
  - `ping`/`pong`: 연결 유지

### `cache/` — Redis 캐시

3단계 캐시 구조:

| 캐시 유형 | 키 패턴 | TTL | 용도 |
|---|---|---|---|
| 타일 캐시 | `tile:{dataset_id}:{chart_hash}:z{zoom}:x{x}:y{y}` | 1시간 | 렌더된 PNG 타일 바이트 |
| 쿼리 캐시 | `query:{dataset_id}:{range_hash}:{algo}:{n}` | 30분 | 다운샘플된 Arrow IPC 바이트 |
| 세션 캐시 | `session:{session_id}` | 15분 | JSON 세션 상태 |

같은 데이터셋 + 같은 차트 설정을 보는 여러 사용자는 동일한 타일 캐시를 공유합니다.

### `core/` — 공통 유틸리티

- `errors.py`: `WebchartError` 기반 에러 계층. 에러 코드별로 적절한 HTTP 상태코드로 자동 변환
- `concurrency.py`: `ThreadPoolExecutor` 기반 렌더 풀. Matplotlib는 async-safe하지 않으므로 별도 스레드에서 실행
- `math_utils.py`: `Viewport` 클래스 (픽셀↔데이터 좌표 양방향 변환), `visible_tile_indices()` (뷰포트에 보이는 타일 인덱스 계산)

---

## API Endpoints

### Health
| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | 서버 상태 확인 |

### Datasets
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/v1/datasets/upload` | 파일 업로드 (multipart). 백그라운드에서 Parquet 변환 |
| POST | `/api/v1/datasets/connect` | 원격 데이터 소스 연결 (protocol + URI + credentials) |
| GET | `/api/v1/datasets` | 전체 데이터셋 목록 조회 |
| GET | `/api/v1/datasets/{id}` | 데이터셋 메타데이터 조회 (행 수, 크기, 상태) |
| GET | `/api/v1/datasets/{id}/schema` | 컬럼 스키마 조회 (타입, min/max, null count) |
| GET | `/api/v1/datasets/{id}/preview?rows=100` | 데이터 미리보기 (최대 1000행) |
| DELETE | `/api/v1/datasets/{id}` | 데이터셋 및 파일 삭제 |

### Charts
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/v1/charts/render` | 정적 차트 렌더링 → 이미지 바이너리 응답 |
| POST | `/api/v1/charts/render/info` | 렌더 메타데이터만 반환 (크기, 포맷, 데이터 포인트 수) |

### Sessions (Dynamic Charts)
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/v1/sessions` | 인터랙티브 세션 생성, WebSocket URL 반환 |
| GET | `/api/v1/sessions/{id}` | 세션 상태 조회 |
| DELETE | `/api/v1/sessions/{id}` | 세션 종료 |
| WS | `/api/v1/ws/{session_id}` | WebSocket 연결 |

### WebSocket Protocol

WebSocket 연결 후 JSON 텍스트 프레임과 바이너리 프레임을 혼합 사용합니다.

**Client → Server (JSON)**
```jsonc
// 뷰포트 변경 (스크롤, 줌)
{ "type": "viewport_change", "x_range": [0, 1000], "y_range": [0, 500], "zoom": 3 }

// 마우스 호버
{ "type": "hover", "pixel_x": 450, "pixel_y": 230 }

// 스크롤 (뷰포트 자동 이동)
{ "type": "scroll", "direction": "right", "delta": 200 }

// 연결 유지
{ "type": "ping" }
```

**Server → Client (JSON)**
```jsonc
// 타일 메타데이터 (몇 개의 타일이 전송될지 미리 알려줌)
{ "type": "tiles_meta", "tiles": [
    { "tile_id": "3-4-0", "x": 4, "y": 0, "width": 512, "height": 512, "ready": true }
]}

// 호버 위치의 데이터 값
{ "type": "hover_data", "data_x": 1234.56, "data_y": 78.9, "series": "price", "label": "x=1234.56" }

// 뷰포트 변경 확인
{ "type": "viewport_ack", "x_range": [0, 1000], "zoom": 3, "tile_count": 6 }

// 크로스헤어 등 오버레이 이미지
{ "type": "overlay", "base64_png": "...", "x": 0, "y": 0, "width": 1200, "height": 600 }

// 에러
{ "type": "error", "code": "SESSION_NOT_FOUND", "message": "..." }
```

**Server → Client (Binary)**
```
타일 이미지: [4바이트 tile_id 길이 (big-endian)][tile_id UTF-8][PNG 바이트]
```

---

## Data Flow

### 정적 차트 렌더링 흐름

```
POST /api/v1/charts/render
        │
        ▼
  ChartSpec 파싱 (차트 유형, 축, 시리즈 설정)
        │
        ▼
  read_window() — Dask로 x축 범위의 파티션만 로드
        │
        ▼
  downsample() — LTTB로 화면 너비x2 포인트로 축소
        │                (예: 1200px → 2400 포인트)
        ▼
  MatplotlibRenderer.render() — ThreadPool에서 PNG 생성
        │
        ▼
  Response: image/png
```

### 동적 차트 인터랙션 흐름

```
POST /api/v1/sessions → { session_id, websocket_url }
        │
        ▼
WS /api/v1/ws/{session_id} 연결
        │
        ▼
Client: viewport_change { x_range, zoom }
        │
        ▼
  visible_tile_indices() — 뷰포트에 보이는 타일 계산
        │
        ▼
  Redis 캐시 확인 (hit → 바로 전송)
        │
  캐시 miss인 타일만:
        │
        ▼
  read_window() + downsample() + render_tile()
        │
        ▼
  Redis에 캐싱 + 바이너리 프레임으로 전송
        │
        ▼
Client: hover { pixel_x, pixel_y }
        │
        ▼
  pixel_to_data() 좌표 변환 → 최근접 포인트 탐색
        │
        ▼
  hover_data JSON 응답
```

---

## Development

### Prerequisites
- Python 3.11+
- Redis
- Docker (optional)

### Setup
```bash
# 의존성 설치
pip install -e ".[dev]"

# Redis 시작
docker run -d -p 6379:6379 redis:7-alpine

# 개발 서버 실행
uvicorn webchart.main:app --reload --host 0.0.0.0 --port 8000
```

### Docker
```bash
docker-compose up -d
# API: http://localhost:8000
# Swagger UI: http://localhost:8000/docs
```

### Testing
```bash
# 전체 테스트
pytest tests/ -v

# 커버리지 포함
pytest tests/ --cov=webchart --cov-report=term-missing
```

### Linting & Type Check
```bash
ruff check src/
ruff format src/
mypy src/webchart/
```

---

## Implementation Status

| Phase | 내용 | 상태 |
|---|---|---|
| Phase 1 | Config, FastAPI 앱, 파일 업로드, Dask 데이터 접근, 다운샘플링 | 완료 |
| Phase 2 | Matplotlib 렌더러, 차트 API, Redis 캐싱 | 완료 |
| Phase 3 | WebSocket, 타일 렌더러, Hover/Scroll 인터랙션 | 완료 |
| Phase 4 | 원격 커넥터 (S3/FTP/SFTP/HTTP/SMB), 추가 차트 유형, 인증, 부하 테스트 | 예정 |

---

## License

MIT
