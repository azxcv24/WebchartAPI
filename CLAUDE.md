# CLAUDE.md - WebchartAPI

## Project Overview

WebchartAPI는 대용량 데이터(수백 GB)를 서버사이드에서 차트로 렌더링하여 제공하는 Python FastAPI 기반 API 서비스.

## Tech Stack

- **Language**: Python 3.11+
- **Framework**: FastAPI (async)
- **Data Processing**: pyarrow (범위 읽기), numpy (집계 커널), Dask (인제스트)
- **Chart Rendering**: Matplotlib Agg (그리기 프로세스 풀), numpy 밀도 래스터
- **Real-time**: WebSocket (starlette)
- **Cache**: Redis
- **Data Format**: Apache Parquet (canonical storage)
- **Container**: Docker + docker-compose

## Project Structure

```
src/webchart/
├── main.py, config.py      # 앱 팩토리(lifespan: 렌더 풀·그리기 프로세스 풀·Redis), 설정
├── api/                    # HTTP·WS 엔드포인트, 인증, /viewer
├── models/                 # Pydantic API 모델 (ChartSpec v2 레이어+축, WS v2 메시지, DatasetMeta)
├── domain/                 # 순수 로직, numpy만, mypy --strict
│   ├── geometry.py         #   XRange, BucketGrid, PixelLattice, TileAddress, ScreenTransform
│   ├── bucket_stats.py     #   BucketStats 모노이드 + 벡터 커널
│   ├── projection.py       #   M4, band, bar, candle, 밀도
│   ├── y_range.py          #   YRange, 정책, matplotlib과 같은 자동 스케일
│   └── ticks.py            #   수치·시간 눈금
├── infrastructure/         # raw_store(정렬 Parquet + zone map), pyramid_store(.npy mmap)
├── application/            # 유스케이스: windows(경로 선택), tiles, static_chart, layers(레지스트리),
│                           #   y_ranges, density, interaction, pyramids, append, live, axes, chart_registry
├── services/
│   ├── ingest/             # 커넥터, sorted_layout(x 정렬 저장), time_columns
│   ├── data/               # access(메타·read_window), x_domain(정규 좌표), downsample(이전 호환 근사)
│   ├── render/             # matplotlib_renderer, painters(레지스트리)
│   └── session/            # manager, stream(WS v2)
├── cache/                  # redis_cache(L2), memory_cache(L1)
├── core/                   # errors, concurrency(스레드·프로세스 풀), metrics
├── testing/                # 합성 데이터, 래스터 비교 (오라클용)
└── web/                    # 레퍼런스 뷰어 (webchart.js, viewer.html)
tests/oracle/               # 무왜곡 오라클 (원본 전체 렌더와 비교)
```

## Key Architecture Concepts

### 픽셀 정렬 집계 (무왜곡의 핵심)
x 전체를 2^L 등분한 격자(PixelLattice)를 피라미드·타일·정적 렌더가 공유한다. 타일 (z, dpr)의
픽셀 열 = 레벨 z + log2(tile_size·dpr)의 버킷 1개. 버킷 경계 = 픽셀 경계이므로 M4가 정확하다.
LTTB 등은 `downsample_algorithm`으로 고를 때만 쓰는 근사다(exact=false).

### 렌더 파이프라인
plan(application/windows.py: 피라미드 | 원본 | 근사) → fetch → project(application/layers.py
레지스트리) → paint(services/render/painters.py 레지스트리, 그리기 프로세스 풀) → L1/L2 캐시.
타일과 정적 렌더가 같은 경로다. 응답에 exact/source를 붙인다.

### y범위
한 프레임의 모든 타일은 축(y, y2)별 y범위 하나를 공유한다(캐시 키 포함). 정책: global / viewport / fixed.

### WebSocket Protocol v2
`/api/v1/ws/{session_id}`. viewport(latest-wins 취소) / hover(원본 값) / select(정확한 구간 통계) /
ping. 서버 → frame_begin(타일·y범위·눈금) · 바이너리 타일(13바이트 헤더) · frame_end ·
crosshair · range_stats · data_appended.

### 실시간 추가
`POST /datasets/{id}/rows` → 새 Parquet 파일 + 피라미드 버킷 in-place merge + 격자 2배 확장 +
영향받은 타일만 캐시 키 변경(tile epoch) + WS 알림.

## Common Commands

```bash
# 설치 (개발용)
pip install -e ".[dev]"

# 개발 서버 (Redis 필요)
uvicorn webchart.main:app --reload --host 0.0.0.0 --port 8000

# 테스트 (오라클 포함)
pytest tests/ -v

# Docker
docker-compose up -d

# 타입 검사 (domain은 strict)
mypy --strict src/webchart/domain

# Lint
ruff check src/ tests/ scripts/
ruff format src/ tests/ scripts/
```

## v2 (무왜곡 엔진) — 반드시 먼저 읽을 것

`docs/improvement-plan.md`가 설계 기준 문서다 (Phase 0~4 완료, 진행 기록은 10장).
- 핵심 목표: 대용량 데이터를 **왜곡 없이** 서버에서 그리고, 브라우저는 DOM 최소(차트당 ≤ 3개), 인터랙션은 WS 스트리밍
- 무왜곡 = 오라클 테스트(`tests/oracle/`, 원본 전체 렌더와 비교)로 검증되는 계약. 근사 경로는 응답에 `exact=false` 표기
- 렌더 경로를 바꾸면 오라클을 반드시 돌린다. 알려진 왜곡은 `tests/oracle/harness.py`의 KNOWN_BUGS(strict xfail)에 등록
- 코드 컨벤션(이름에 단위, `[start, end)` 범위, 한국어 docstring, 패턴 적용 기준)은 계획 문서 6장을 따른다

## Conventions

- 모든 API 엔드포인트는 `/api/v1/` 접두사 사용
- Pydantic v2 모델 사용 (request/response schemas)
- 비동기 I/O 우선. 막히는 작업은 `run_in_thread`(렌더 스레드 풀), matplotlib 그리기는 `paint`(프로세스 풀)
- 데이터 수집 커넥터는 `IngestBackend` 프로토콜 구현
- 새 차트 종류는 `application/layers.py`의 PROJECTIONS와 `services/render/painters.py`의 PAINTERS에 등록
- 캐시 키 형식: `{type}:{dataset_id}:{...}` (타일: `tile:{dataset}:{data_version}:{그림 해시}:{z}-{i}@{dpr}x:e{세대}`)
- 에러 응답은 `core/errors.py`의 커스텀 예외 계층 사용
- 환경 변수 기반 설정 (Pydantic Settings)

## Environment Variables

모든 설정은 `WEBCHART_` 접두사 환경변수로 바꾼다. 전체 목록과 설명은 `src/webchart/config.py`와 README에 있다.

```
WEBCHART_REDIS_URL=redis://localhost:6379/0
WEBCHART_DATA_DIR=/data/webchart          # 원본 Parquet·피라미드 저장 경로
WEBCHART_TILE_SIZE=512                    # 타일 픽셀 크기 (2의 거듭제곱)
WEBCHART_PAINT_PROCESSES=8                # matplotlib 그리기 프로세스 수 (0이면 스레드)
WEBCHART_API_KEYS='["key1"]'              # 비우면 인증 꺼짐 (개발용)
WEBCHART_LOCAL_INGEST_ROOT=/srv/shared    # file:// 인제스트 허용 디렉터리 (비우면 거부)
```
