# WebchartAPI

수백 GB 데이터를 **왜곡 없이**, 원하는 차트로, API 하나로 어디서든 그리는 서버사이드 차트 엔진.

- **무거운 일은 서버가 한다.** RAM이 큰 서버가 집계와 렌더링을 맡는다.
- **브라우저는 거의 아무것도 하지 않는다.** 레퍼런스 뷰어의 DOM은 차트당 3개(컨테이너 + 캔버스 2개)다.
- **인터랙션은 스트리밍이다.** 줌·이동·호버·구간 통계를 WebSocket으로 그때그때 받아 온다.
- **"왜곡 없음"은 테스트로 강제한다.** 서버가 그린 그림을 원본 전체를 그린 그림과 픽셀 단위로 비교한다(오라클 테스트).

설계 근거, 측정 기록, 결정 사항은 [docs/improvement-plan.md](docs/improvement-plan.md)에 있다.

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

Python 3.11 이상과 Redis가 필요하다.

```bash
# Docker로 (API + Redis)
docker-compose up -d

# 또는 직접 설치
pip install -e .                    # 원격 커넥터(S3·FTP·SFTP·SMB·ClickHouse)까지: ".[connectors]"
docker run -d -p 6379:6379 redis:7-alpine
WEBCHART_DATA_DIR=./data uvicorn webchart.main:app --host 0.0.0.0 --port 8000
```

```bash
# 1. 업로드 (CSV, Parquet, JSON, Excel, TSV) → 백그라운드에서 정렬된 Parquet으로 변환
curl -F file=@prices.csv http://localhost:8000/api/v1/datasets/upload
# {"dataset_id": "3f2c…", "status": "ingesting"}

# 2. (큰 데이터라면) 피라미드 빌드: 줌아웃 화면을 미리 집계한 레벨에서 정확하게 그린다
curl -X POST http://localhost:8000/api/v1/datasets/3f2c…/pyramid \
     -H 'Content-Type: application/json' -d '{"x_column": "ts"}'

# 3. 이미지 한 장
curl -X POST http://localhost:8000/api/v1/charts/render -H 'Content-Type: application/json' \
     -d '{"dataset_id": "3f2c…", "x_axis": {"column": "ts"},
          "layers": [{"kind": "line", "column": "price"}]}' -o chart.png
```

4. 인터랙티브: 브라우저에서 `http://localhost:8000/viewer/`를 연다. 드래그로 이동, 휠로 줌, Shift+드래그로 구간 통계를 본다.
   뷰어는 `src/webchart/web/webchart.js` 하나(프레임워크 없음, 약 14KB)라 다른 페이지에도 그대로 넣을 수 있다.

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
| `MAX_UPLOAD_SIZE` | 10GB | 업로드·원격 다운로드 크기 상한 |
| `API_KEYS` | 비움 | API 키 목록 (JSON 배열). 비우면 인증 꺼짐 (개발용) |
| `EMBED_SECRET` | 비움 | 임베드 토큰 HMAC 비밀값. 비우면 `API_KEYS`에서 만든다 |
| `CORS_ALLOW_ORIGINS` | `["http://localhost:3000"]` | 허용할 브라우저 출처 |
| `RATE_LIMIT_PER_MINUTE` | 120 | API 키(없으면 IP)별 분당 요청 수 |
| `BLOCK_PRIVATE_EGRESS` | `true` | 원격 커넥터가 사설·루프백 주소에 접속하지 못하게 막는다 (SSRF 방지) |
| `LOCAL_INGEST_ROOT` | 비움 | `file://` 인제스트가 읽을 수 있는 디렉터리. 비우면 `file://` 거부 |

전체 목록과 설명은 [src/webchart/config.py](src/webchart/config.py)에 있다.

---

## 보안

공개된 곳에 띄우기 전에 확인할 것.

- **인증을 켠다.** `WEBCHART_API_KEYS`가 비어 있으면 누구나 업로드·조회·삭제할 수 있다.
- **원격 커넥터**는 서버가 대신 외부에 접속하는 기능이다.
  - 기본으로 사설·루프백·링크로컬 주소를 막는다(`BLOCK_PRIVATE_EGRESS`). 내부 MinIO처럼 의도한 경우에만 끈다.
  - HTTP 리다이렉트가 다른 호스트로 가면 인증 헤더를 보내지 않는다.
- **`file://` 인제스트**는 `LOCAL_INGEST_ROOT`로 지정한 디렉터리 안만 읽는다(기본 꺼짐).
- **브라우저 WebSocket**은 헤더를 붙일 수 없어 API 키를 `?api_key=` 쿼리로도 받는다. 이 값은 프록시·접근 로그에 남을 수 있으니, 공개 환경에서는 로그에서 쿼리를 지우거나 짧게 쓰는 키를 쓴다.
- **임베드·타일 URL**에는 API 키 대신 차트 하나에만 유효한 HMAC 토큰이 붙는다. 키를 바꾸면 기존 URL이 무효가 되므로 운영에서는 `EMBED_SECRET`을 따로 지정한다.

보안 문제를 발견하면 공개 이슈 대신 저장소 관리자에게 먼저 알려 주면 좋겠다.

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

`tests/oracle/`는 합성 데이터(스파이크, 결측, 불규칙 간격, 다중 시리즈, 시간축, 실시간 추가)로 서버 결과를 원본 전체 렌더와 비교한다. 비교 규칙(세로 1px, 선 굵기 번짐 가로 1열)은 계획 문서 8장에 있다.

기여할 때
- 렌더 경로(집계·투영·그리기)를 바꾸면 오라클을 반드시 돌린다. 근사가 생기면 응답에 `exact=false`로 드러나야 한다.
- 코드 컨벤션은 계획 문서 6장을 따른다: 주석·docstring은 한국어, 식별자는 영어, 이름에 단위(`width_px`, `x_ns`), 범위는 반열린 구간 `[start, end)`.
- `domain/`은 numpy만 쓰고 `mypy --strict`를 통과해야 한다.

---

## 라이선스

[MIT](LICENSE). 상업적 이용을 포함해 자유롭게 쓰고 고치고 배포할 수 있다. 복사본에는 저작권·라이선스 문구를 남겨야 한다.

포크하거나 일부를 가져다 쓸 때 이 저장소(https://github.com/azxcv24/WebchartAPI)를 출처로 밝혀 주면 고맙겠다 (라이선스 의무가 아니라 부탁이다).

### 함께 쓰는 오픈소스와 참고한 연구

이 프로젝트는 다음 오픈소스 위에서 만들어졌다. 각 라이선스는 해당 프로젝트를 따른다.

| 프로젝트 | 쓰는 곳 | 라이선스 |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi), [Starlette](https://github.com/encode/starlette), [Pydantic](https://github.com/pydantic/pydantic), [Uvicorn](https://github.com/encode/uvicorn) | API 서버, WebSocket, 설정 | MIT / BSD-3-Clause |
| [Apache Arrow (pyarrow)](https://github.com/apache/arrow) | Parquet 범위 읽기, 정규 좌표 변환 | Apache-2.0 |
| [NumPy](https://github.com/numpy/numpy), [pandas](https://github.com/pandas-dev/pandas), [Dask](https://github.com/dask/dask) | 집계 커널, 인제스트 | BSD-3-Clause |
| [Matplotlib](https://github.com/matplotlib/matplotlib), [Pillow](https://github.com/python-pillow/Pillow) | 그리기, PNG 인코딩 | Matplotlib License (PSF 기반) / MIT-CMU |
| [redis-py](https://github.com/redis/redis-py), [HTTPX](https://github.com/encode/httpx) | 캐시·세션, HTTP 커넥터 | MIT / BSD-3-Clause |

- **M4 집계**: U. Jugel, Z. Jerzak, G. Hackenbroich, V. Markl, "M4: A Visualization-Oriented Time Series Data Aggregation", *PVLDB* 7(10), 2014.
- **밀도 정규화(eq_hist)**는 [Datashader](https://github.com/holoviz/datashader)의 방식을 참고했다.
