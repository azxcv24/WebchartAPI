# WebchartAPI

수백 GB 데이터를 **왜곡 없이**, 원하는 차트로, API 하나로 어디서든 그리는 서버사이드 차트 엔진.

- **무거운 일은 서버가 한다.** RAM이 큰 서버가 집계와 렌더링을 맡는다.
- **브라우저는 거의 아무것도 하지 않는다.** 레퍼런스 뷰어의 DOM은 차트당 3개(컨테이너 + 캔버스 2개)다.
- **인터랙션은 스트리밍이다.** 줌·이동·호버·구간 통계를 WebSocket으로 그때그때 받아 온다.
- **"왜곡 없음"은 테스트로 강제한다.** 서버가 그린 그림을 원본 전체를 그린 그림과 픽셀 단위로 비교한다(오라클 테스트).

설계와 진행 기록은 [docs/improvement-plan.md](docs/improvement-plan.md)에 있다.

---

## 왜 왜곡이 없는가

### 픽셀 정렬 집계 (M4)

선 차트는 **픽셀 열마다 첫 점·최솟값·최댓값·끝 점(M4)** 만 남겨 그린다. 집계 구간(버킷) 경계가 픽셀 경계와 정확히 같으면, 이 네 점으로 그린 그림은 원본 전체로 그린 그림과 같다(Jugel et al., PVLDB 2014). 서버 전체가 이 조건 하나를 지키도록 설계되어 있다.

```
x 전체 [x0, x1) 를 2^L 등분 = 피라미드 레벨 L
타일 (줌 z, dpr) = 레벨 z + log2(타일 px × dpr) 의 연속 버킷 → 버킷 1개 = 픽셀 열 1개
```

### 하나의 집계 구조: BucketStats

버킷마다 `count, sum, first, last, min(+x), max(+x), 결측 위치`를 저장한다. 두 버킷을 합쳐도(merge) 정확하게 다시 계산되는 모노이드라서 다음이 모두 가능하다.

- 세밀한 레벨을 2개씩 합쳐 상위 레벨을 정확히 만든다(피라미드)
- 실시간으로 추가된 행을 다시 집계하지 않고 버킷에 더한다
- 하나의 저장 구조에서 모든 차트를 만든다

| 레이어 | 투영 | 정확성 |
|---|---|---|
| line / area | M4 | 원본 래스터와 같음 |
| band | 픽셀 열별 min~max | 열별 값이 원본과 같음 |
| bar | 구간별 sum / mean / count / min / max (명시) | 구간 원본 행의 정확한 집계 |
| candlestick | 구간별 OHLC (가격 컬럼 또는 OHLC 컬럼) | 정확 |
| scatter / heatmap | 픽셀별 점 개수 (2D 밀도, linear/log/eq_hist) | 개수가 원본과 같음 |

정확하지 않은 경로(예: 원본이 예산보다 커서 피라미드를 픽셀로 다시 묶는 정적 렌더)를 타면 응답 헤더에 `X-Webchart-Exact: false`와 출처(`X-Webchart-Source`)를 붙인다.

---

## 아키텍처

```
api/            HTTP · WebSocket · 인증 · 임베드          ─┐
application/    유스케이스: 경로 선택, 타일·정적 렌더,       │ 바깥 → 안쪽으로만 의존
                y범위 정책, 인터랙션, 실시간 추가            │
domain/         순수 로직 (numpy만): 격자, BucketStats,    ◀┘ mypy --strict
                투영(M4·막대·캔들·밀도), y범위, 눈금
infrastructure/ 원본 저장소(zone map), 피라미드 저장소(mmap)
services/       인제스트 커넥터, matplotlib 페인터, 세션
```

렌더 파이프라인은 하나다(타일과 정적 렌더가 같은 경로).

```
plan     격자·레벨·축별 y범위·exact 여부 결정       application/windows.py, y_ranges.py
fetch    피라미드 레벨 | 원본 범위 집계              infrastructure/pyramid_store.py, raw_store.py
project  레이어 레지스트리 → 도형                     application/layers.py, domain/projection.py
paint    도형 레지스트리 → matplotlib (프로세스 풀)   services/render/painters.py
cache    L1 프로세스 메모리 → L2 Redis (데코레이터)   application/tiles.py
```

### 저장 구조

| 계층 | 내용 |
|---|---|
| 원본 | 축 컬럼으로 **정렬**된 Parquet, 131,072행 로우그룹. 로우그룹별 x 범위(zone map)로 필요한 범위만 읽는다 |
| 피라미드 | 레벨별 `.npy` 배열을 mmap으로 연다. OS 페이지 캐시가 그대로 메모리 계층이 된다. 실시간 추가는 같은 파일을 쓰기 가능한 mmap으로 고친다 |
| L1 캐시 | 프로세스 메모리 바이트 상한 LRU (렌더된 타일) |
| L2 캐시 | Redis (타일, 세션) |

### 경로 선택

1. 피라미드 레벨 경계가 요청 격자와 맞으면 → 피라미드 (정확). 타일은 항상 여기에 해당한다.
2. 원본 읽기량이 예산(`EXACT_RAW_ROW_BUDGET`) 안이거나 피라미드가 없으면 → 원본을 로우그룹씩 읽어 즉석 집계 (정확, 메모리 = 격자 + 로우그룹 1개)
3. 그 외 → 피라미드를 픽셀로 다시 묶음 (근사, `exact=false`)

---

## 빠르게 써 보기

```bash
pip install -e ".[dev]"
uvicorn src.webchart.main:app --reload --host 0.0.0.0 --port 8000   # Redis 필요
# 또는
docker-compose up -d
```

1. 업로드: `POST /api/v1/datasets/upload` (CSV, Parquet, JSON, Excel, TSV)
2. 피라미드(선택, 큰 데이터): `POST /api/v1/datasets/{id}/pyramid {"x_column": "ts"}`
3. 브라우저에서 `http://localhost:8000/viewer/`: 드래그 이동 · 휠 줌 · Shift+드래그 구간 통계

### 차트 명세 (ChartSpec v2)

```jsonc
{
  "dataset_id": "…",
  "x_axis": { "column": "ts", "timezone": "Asia/Seoul" },
  "layers": [
    { "kind": "candlestick", "column": "price", "group_px": 16 },
    { "kind": "line", "column": "ma20", "style": { "color": "#e45" } },
    { "kind": "bar", "column": "volume", "agg": "sum", "axis": "y2" }
  ],
  "axes": { "y": { "label": "가격" }, "y2": { "label": "거래량", "range_policy": "viewport" } },
  "width": 1200, "height": 600
}
```

- y범위 정책: `global`(전체 데이터, 캐시 효율 최고) · `viewport`(보이는 구간) · `fixed`(`range` 지정)
- 이전 형식(`chart_type`, `y_columns`, `series`, `y_axis`)도 그대로 받는다(레이어로 자동 변환).
- 시간축 눈금은 표시 시간대의 벽시계에 맞춘다: ① `x_axis.timezone` ② 컬럼의 원래 시간대 ③ 시간대 없는 컬럼은 저장된 값 그대로.

---

## API

### 데이터셋
| Method | Endpoint | 설명 |
|---|---|---|
| POST | `/api/v1/datasets/upload` | 파일 업로드. 정렬된 Parquet으로 변환 (백그라운드) |
| POST | `/api/v1/datasets/connect` | 원격 소스 (S3, HTTP, FTP, SFTP, SMB, ClickHouse, 로컬) |
| GET | `/api/v1/datasets`, `/{id}`, `/{id}/schema`, `/{id}/preview` | 조회 |
| POST | `/api/v1/datasets/{id}/pyramid` | 피라미드 빌드 (BucketStats + 2D 밀도) |
| POST | `/api/v1/datasets/{id}/rows` | **실시간 추가** `{"columns": {"ts": [...], "price": [...]}}` |
| DELETE | `/api/v1/datasets/{id}` | 삭제 |

### 차트
| Method | Endpoint | 설명 |
|---|---|---|
| POST | `/api/v1/charts/render` | 이미지 한 장. 헤더 `X-Webchart-Exact`, `X-Webchart-Source`, `X-Data-Points` |
| POST | `/api/v1/charts` | 차트 등록 → `chart_id`(spec 내용 해시), 임베드 URL |
| GET | `/api/v1/charts/{id}/image.png?x0&x1&w&h` | `<img src>` 임베드. ETag/304 |
| GET | `/api/v1/charts/{id}/tiles/{z}/{i}@{dpr}x.png` | XYZ 타일 (CDN 캐시 가능) |

인증이 켜져 있으면(`WEBCHART_API_KEYS`) 임베드·타일 URL에는 API 키 대신 차트별 HMAC 토큰이 붙는다.

### 인터랙티브 세션 (WebSocket 프로토콜 v2)

`POST /api/v1/sessions` → `WS /api/v1/ws/{session_id}`

| 클라이언트 → 서버 | 서버 동작 |
|---|---|
| `viewport {req_id, x_range, zoom?, width_px, height_px, dpr}` | 진행 중인 이전 프레임 취소(latest-wins) → `frame_begin` → 타일(보이는 것부터, 준비되는 대로) → `frame_end` |
| `hover {req_id, pixel_x}` | `crosshair`: 시리즈마다 가장 가까운 **원본** 값 |
| `select {req_id, x_range}` | `range_stats`: 구간 count/min/max/mean/first/last (정확) |
| `ping` | `pong` |

- `frame_begin`에는 타일 목록, 축별 공유 y범위, **서버가 계산한 x/y 눈금**이 들어간다.
- 데이터가 추가되면 `data_appended`가 온다(바뀐 x 구간). 바뀌지 않은 타일은 캐시 키가 그대로라 바로 나온다.
- 타일 바이너리 헤더(13바이트, big-endian): `[magic 0x57][version 2][req_id u32][zoom u8][index u32][dpr u8][format u8]` + PNG

### 운영
| Endpoint | 설명 |
|---|---|
| `/health` | 상태 |
| `/metrics` | Prometheus: 타일 요청(l1_hit / l2_hit / rendered), 렌더 지연, 경로별 exact 수 |

---

## 설정 (환경변수, `WEBCHART_` 접두사)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | L2 캐시·세션 |
| `DATA_DIR` | `/data/webchart` | 데이터 저장 경로 |
| `TILE_SIZE` | `512` | 타일 px (2의 거듭제곱) |
| `RENDER_POOL_SIZE` | `8` | fetch/project 스레드 수 |
| `PAINT_PROCESSES` | `min(8, CPU/2)` | matplotlib 그리기 프로세스 수 (0이면 스레드에서 그림) |
| `TILE_MEMORY_CACHE_BYTES` | 512MB | L1 타일 캐시 상한 |
| `PYRAMID_MAX_LEVEL` / `PYRAMID_MAX_BYTES` | 24 / 4GB | 피라미드 깊이·크기 상한 |
| `EXACT_RAW_ROW_BUDGET` | 20,000,000 | 정적 렌더에서 원본을 직접 집계할 행 수 상한 |
| `INGEST_ROW_GROUP_ROWS` | 131,072 | 원본 로우그룹 크기 |
| `API_KEYS`, `EMBED_SECRET` | 비움 | 인증 키, 임베드 토큰 비밀값 |

---

## 성능 (16코어, RAM 125GB, 캐시 없이 처음 그리기, 시리즈 2개)

| 단계 | 2,000만 행 | 1억 행 |
|---|---:|---:|
| 인제스트 (정렬 검사 + 로우그룹) | 6.5s | 17.4s |
| 피라미드 빌드 | 2.3s (420MB) | 17.8s (1.9GB) |
| 타일 z=0 (피라미드) | 0.03s | 0.03s |
| 깊은 줌 타일 (원본 범위 읽기) | 0.02s | 0.03s |
| 정적 렌더 전체 범위 | 1.2s (원본, exact) | 0.11s (피라미드, 근사) / 5.6s (원본, exact) |
| 타일 64장 프레임 (그리기 프로세스 8) | — | 0.28s |

1억 행에서 뷰어로 휠 줌·드래그 이동을 2.8초 동안 40번(viewport 요청 72개) 몰아 보내도, 그 사이 프레임은 모두
취소되고 마지막 입력 0.26초 뒤에 최종 프레임(타일 6장, exact)이 도착했다. DOM 노드는 3개였다.
측정 방법과 단계별 기록은 [docs/improvement-plan.md](docs/improvement-plan.md) 10장과 `scripts/bench/`에 있다.

---

## 개발

```bash
pytest tests/ -v                 # 오라클 포함 (tests/oracle/)
ruff check src/ && mypy --strict src/webchart/domain
PYTHONPATH=src python scripts/bench/bench_pipeline.py --rows 20000000 --series 2
PYTHONPATH=src python scripts/bench/bench_render_parallelism.py
```

`tests/oracle/`는 합성 데이터(스파이크, 결측, 불규칙 간격, 다중 시리즈, 시간축)로 서버 결과를 원본 전체 렌더와 비교한다. 비교 규칙(세로 1px, 선 굵기 번짐 가로 1열)은 계획 문서 8장에 있다.
