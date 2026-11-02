# WebchartAPI v2 개선 계획 — 무왜곡 대용량 차트 엔진

> 작성: 2026-11-02 · 상태: **Phase 0~4 완료** (진행 기록은 10장)
> 출발점: Gemini 리뷰(2026-10-29) + 코드 직접 검증 결과

---

## 0. 목표

> **수백 GB 데이터를, 왜곡 없이, 원하는 형태의 차트로, 어떤 클라이언트에서든 API 하나로 그린다.**

- **무거운 일은 서버가 한다.** RAM이 큰 단일 서버가 집계와 렌더링을 맡는다.
- **브라우저는 거의 아무것도 하지 않는다.** 데이터 포인트마다 DOM 노드를 만들지 않는다. 차트 하나에 DOM 노드는 3개 이하, 메인 스레드 작업은 이미지 합성뿐이다.
- **인터랙션은 스트리밍으로 한다.** 줌, 이동, 호버 결과를 필요할 때마다 서버에서 받아온다.

### 설계 원칙

| # | 원칙 | 의미 |
|---|---|---|
| P1 | **무왜곡은 측정 가능한 계약이다** | "집계해서 그린 그림 = 원본 전체를 그린 그림"을 픽셀 비교 테스트(오라클)로 강제한다. 감으로 판단하지 않는다. |
| P2 | **집계 단위는 '행 개수'가 아니라 '출력 픽셀 열'이다** | 모든 다운샘플링 버킷 경계를 실제 그려질 픽셀 경계에 맞춘다. |
| P3 | **근사일 때는 근사라고 말한다** | 정확하지 않은 경로를 탔다면 응답에 `exact=false`와 사용한 데이터 출처를 표기한다. |
| P4 | **값은 원본에서 읽는다** | 호버나 구간 통계에 보이는 숫자는 집계값이 아니라 원본 행에서 가져온다. |
| P5 | **빠진 데이터는 빠진 대로 보여준다** | 결측 구간을 선으로 이어 숨기지 않는다. 기본 정책은 `break`. |
| P6 | **서버 RAM을 적극적으로 쓴다** | 집계 피라미드는 메모리에 상주시키고(mmap), 원본은 필요한 범위만 읽는다. |
| P7 | **최신 요청이 이긴다** | 새 뷰포트 요청이 오면 이전 렌더는 취소한다. 보이는 타일을 먼저 보내고 프리페치는 나중에 보낸다. |

---

## 1. 현재 상태 진단 (2026-11-02 검증)

테스트 75개는 모두 통과한다. 그런데 아래 문제는 테스트가 잡지 못하고 있었다. 모두 직접 재현했다.

| ID | 문제 | 근거 / 재현 결과 | 영향 |
|---|---|---|---|
| ~~D1~~ ✅ | 타일마다 y축 스케일이 다르다 (Phase 1.6에서 해결) | [tile_renderer.py:112](../src/webchart/services/render/tile_renderer.py#L112)에서 `render_tile`에 `y_range`를 넘기지 않는다. 캐시 키에도 y범위가 없다. | 타일을 이어 붙이면 경계에서 선이 어긋남 |
| ~~D2~~ ✅ | 2번째 이후 시리즈의 피크가 사라진다 (Phase 1.5에서 해결) | `downsample_multi_series`가 첫 y컬럼만 기준으로 점을 고른다. 100만 행에 넣은 2번째 시리즈 스파이크 **20개 중 0개** 보존 (lttb, minmax 둘 다). | 다중 시리즈 차트 전부 |
| ~~D3~~ ✅ | 시간 x축이 동작하지 않는다 (Phase 0.5에서 해결) | CSV의 날짜는 `large_string`으로, Parquet의 timestamp는 `Timestamp` 객체로 저장된다. 둘 다 `_resolve_x_range`가 `None`을 반환해 x범위가 0~1로 대체된다. | 시계열(주 용도) 타일 세션 전체 |
| ~~D4~~ ✅ | 다운샘플 버킷이 행 개수 기준이다 (Phase 1.1·1.4에서 해결) | `_minmax`, `_average`, LTTB 모두 행 인덱스로 버킷을 나눈다. 샘플링 간격이 불규칙하면 픽셀과 버킷이 어긋난다. LTTB는 단일 스파이크 50개 중 45개만 보존했다. | 정확성 보장 불가 |
| ~~D5~~ ✅ | 타일 경로가 피라미드 envelope를 다시 깎는다 (Phase 1.5에서 해결) | 피라미드는 모든 시리즈의 min/max를 보존하는데, 타일 렌더러가 그 결과에 첫 컬럼 기준 LTTB를 한 번 더 적용한다. | D2와 같은 손실 |
| ~~D6~~ ✅ | 범위 읽기가 전체 스캔이다 (Phase 1.2에서 해결) | `read_window`가 Dask에서 필터를 뒤늦게 걸고, `shape[0].compute()`와 `compute()`로 두 번 스캔한다. 인제스트 시 x 정렬도 없다. | 수백 GB에서 깊은 줌 타일 1장 = 전체 스캔 2회 |
| ~~D7~~ ✅ | Candlestick과 Heatmap이 미구현이다 (Phase 2.2·2.4에서 해결) | 각각 line, scatter로 대체되고 경고만 남긴다. | 기능 누락 |
| ~~D8~~ ✅ | WS 처리가 직렬이고 취소가 없다 (Phase 3.1에서 해결) | 모든 타일을 `gather`로 다 그린 뒤에 전송을 시작한다. 빠른 패닝 시 요청이 쌓인다. 타일 크기 512는 하드코딩되어 있다. 호버는 `y_range`가 없으면 (0,1)로 가정하고, 첫 시리즈만 반환한다. | 체감 랙, 잘못된 호버 |
| ~~D9~~ ✅ | 좌표 계산이 4곳에 흩어져 있다 (Phase 0.4에서 해결) | `math_utils`, `tile_renderer`, `interaction`, `pyramid`이 각자 x→타일→픽셀 변환을 한다. | 버그 재발의 구조적 원인 |
| ~~D10~~ ✅ | 차트 타입이 if/elif로 분기된다 (Phase 2.2에서 해결) | `_draw_series`의 분기 구조. | 차트를 추가할 때마다 수정 범위가 커짐 |

> 참고: Gemini 리뷰의 "LTTB는 최고점·최저점을 100% 보존한다"는 서술은 틀렸다(D4).
> LTTB는 시각적 휴리스틱이고, 무왜곡을 보장하는 방법은 아래 2장의 M4다.

---

## 2. 핵심 아이디어: 픽셀 정렬 집계

### 2.1 M4 — 라인 차트의 무왜곡 정리

**M4**(Jugel et al., *PVLDB 7(10)*, 2014): 픽셀 열마다 **first, last, min, max** 네 점만 남겨 선을 그리면,
원본 전체로 그린 래스터 선 그림과 **픽셀 단위로 동일**하다.
조건은 하나다. **버킷 경계가 픽셀 열 경계와 정확히 일치해야 한다.**

그래서 v2의 모든 설계는 이 조건 하나를 지키는 데 맞춰져 있다.

### 2.2 하나의 격자 (Pixel Lattice)

x 전체 범위 `[x0, x1)`를 `W`라 하고, 타일 크기를 `T = 2^t` 픽셀(기본 512 = 2^9)이라 하자.

```
피라미드 레벨 L  : x 전체를 2^L 개 버킷으로 균등 분할      bucket_width = W / 2^L
타일 (z, i)      : x 전체를 2^z 개 타일로 분할, i번째 타일    tile_width   = W / 2^z
타일 픽셀 열      : 타일 하나 = T·dpr 개 열                  pixel_width  = W / 2^(z + t + log2 dpr)

⇒ 레벨 L = z + t + log2(dpr) 를 읽으면 버킷 1개 = 픽셀 열 1개 → M4가 정확히 성립
```

피라미드, 타일, 호버, 축 눈금이 **모두 이 격자 하나를 공유**한다(D9 해소). 이 격자를 표현하는 값 객체가 `PixelLattice`다.

`T`와 `dpr`은 2의 거듭제곱으로 제한한다. 설정 검증 단계에서 강제한다.

### 2.3 BucketStats — 합쳐도 정확한 집계 (모노이드)

버킷마다, 시리즈마다 다음 값을 저장한다.

```
count, sum,
first_x, first_y,  last_x, last_y,
min_y, min_x,      max_y, max_x          # min/max가 실제로 일어난 x까지 저장
```

- `merge(a, b)`는 결합법칙을 만족한다. 그래서 **세밀한 레벨을 합쳐 상위 레벨을 정확히** 만들 수 있고,
  나중에 실시간으로 데이터를 append할 때도 증분 갱신이 가능하다(Phase 4).
- 한 저장 구조에서 모든 차트 투영을 만들 수 있다.

| 차트 | BucketStats에서의 투영 | 정확성 |
|---|---|---|
| line | M4: (first, min, max, last)를 x순으로 | 픽셀 정확 (M4 정리) |
| area / band | min~max envelope | 정확 |
| candlestick | Open=first, High=max, Low=min, Close=last | 정확 (원시 틱 → 캔들 직접 생성) |
| bar | sum / mean / count 중 명시한 집계 | 정확 (집계 방식을 API에 명시) |
| scatter / heatmap | 별도: 2D 카운트 격자 (§2.6) | 픽셀당 카운트 정확 |

로그 스케일은 단조 변환이라 min/max가 보존되므로 그대로 성립한다.

### 2.4 레벨 선택과 원본 대체 경로

```
타일 (z, dpr) 요청
  ├─ 필요 레벨 L ≤ 피라미드 최대 레벨 → 피라미드 L에서 해당 범위 버킷 슬라이스 (O(픽셀 수))
  └─ L > 최대 레벨 (깊은 줌)          → 원본에서 그 타일 범위만 읽어 즉석 M4 (범위가 좁아 저렴)
```

정적 렌더(`/charts/render`)는 임의의 x범위와 폭이 들어오므로 격자에 정렬되지 않는다.

- 원본 읽기 비용이 예산 안이면 원본으로 즉석 M4를 만든다 → `exact=true`
- 아니면 픽셀 폭의 1/8 이하 버킷 레벨을 써서 픽셀 열로 재병합한다 → `exact=false`
  - 경계에 걸친 버킷 1개 이내의 오차. min/max는 실제 x로 배치하므로 위치 오차는 없다.

### 2.5 y범위 정책 (D1 해소)

타일들은 반드시 같은 y범위를 공유한다. y범위는 캐시 키의 일부다.

| 정책 | 동작 |
|---|---|
| `global` (기본) | 데이터셋 전체 min/max에서 계산. 캐시 효율 최고 |
| `viewport` | 피라미드에서 뷰포트 구간의 min/max를 O(픽셀) 비용으로 계산. 캐시 적중을 위해 "nice" 경계로 스냅 |
| `fixed` | 사용자가 지정 |

### 2.6 산점도·히트맵 — 밀도 래스터

점 수백만 개를 겹쳐 찍으면 겹친 양이 사라져서 그 자체로 왜곡이다.
그래서 (x, y)를 픽셀 격자로 2D 비닝해 **픽셀당 개수**를 세고, 정규화(linear / log / eq_hist)는 명시적으로 선택해 컬러맵을 적용한다(Datashader 방식).

- 2D 카운트 피라미드를 전역 (x, y) 범위에 사전 계산한다. 4096² 셀 × 4B = 64MB/레벨이라 RAM에 상주시켜도 부담이 작다.
- 깊은 줌에서는 원본 범위를 읽어 즉석 비닝한다.
- 그리기는 matplotlib 없이 numpy RGBA → PNG로 직접 한다.

### 2.7 원본 저장소 — 범위 읽기를 O(범위)로 (D6 해소)

- **인제스트 불변식:** 인덱스 x컬럼 기준으로 정렬해 저장한다.
  이미 정렬된 소스(대부분의 시계열)는 파티션별 단조성만 검사하고 정렬을 생략한다.
- **로우그룹 통계:** 약 1M 행 단위 로우그룹을 만들고, 로우그룹별 x 최소·최대를 담은 zone map을 메모리에 둔다.
  범위 질의는 이분 탐색으로 로우그룹을 고른 뒤 그 로우그룹만 읽는다(pyarrow).
- **x 정규화 (Phase 0.5에서 구현):** 시간 문자열 컬럼은 인제스트 시 timestamp로 파싱한다.
  - 오프셋이 있는 값은 UTC timestamp로, 오프셋이 없는 값은 naive timestamp(벽시계 값 유지)로 저장한다.
  - 내부 연산은 정규 좌표(float64, 시간은 epoch-ns)로 한다(`services/data/x_domain.py`). 해상도는 약 256ns다.
  - 표시 시점에 tz-aware 컬럼은 원본 시간대로 바꿔 보여준다.
- **데이터 버전:** 인제스트마다 `data_version`을 발급하고 모든 캐시 키에 넣는다.
  재인제스트되면 키가 자연스럽게 바뀌어 무효화된다. 현재의 `usable_pyramid` 지문 휴리스틱을 대체한다.

### 2.8 메모리 계층 (P6)

| 계층 | 내용 | 구현 |
|---|---|---|
| L0 OS 페이지 캐시 | 피라미드 레벨 파일 | **.npy + mmap** → 파이썬 힙 밖에서 제로카피. 큰 RAM이 자연스럽게 캐시가 된다. (처음에는 Arrow IPC였으나 실시간 추가가 버킷을 제자리에서 고쳐야 해서 쓰기 가능한 mmap이 되는 .npy로 바꿨다, Phase 4.3) |
| L1 프로세스 내 | 렌더된 타일 바이트, 원본 zone map | 바이트 상한 LRU (`WEBCHART_MEMORY_BUDGET_*`) |
| L2 Redis | 타일 바이트, 세션 | 기존 유지. 프로세스 재시작과 다중 워커 사이에서 공유 |

피라미드 크기: 버킷 하나 = 8B(행 수) + 시리즈당 96B(필드 12개). 모든 레벨 합은 최상세의 약 2배다.
최대 레벨은 세 가지 중 가장 작은 값이다 (`choose_max_level`).
- 설정 상한 `pyramid_max_level`(24)
- 메모리 예산 `pyramid_max_bytes`(4GB)
- 버킷당 평균 16행 이상: 그보다 세밀하면 피라미드가 원본보다 커진다. 더 깊은 줌은 원본 범위 읽기로 충분히 싸다.

---

## 3. 목표 아키텍처

### 3.1 계층과 의존 방향

```
api/            전송 계층: HTTP, WebSocket, 인증, 직렬화          ──┐
  │                                                               │ 바깥 → 안쪽으로만 의존
application/    유스케이스: RenderChart, RenderTile, InteractionSession │
  │                                                               │
domain/         순수 로직: 격자, BucketStats, 투영, y범위 정책      ◀─┘ (I/O 없음, numpy만)
  ▲
infrastructure/ 구현체: RawStore(parquet), PyramidStore(arrow ipc),
                TileCache(LRU+Redis), Painter(matplotlib/numpy), Encoder(png/webp)
```

- **규칙:** `domain/`은 fastapi, redis, matplotlib, dask를 import하지 않는다. 그래서 순수 단위 테스트와 속성 테스트가 가능하다.
- 기존 `services/` 구조는 한 번에 뒤엎지 않는다. Phase마다 이동한다(Strangler 패턴). 이동이 끝난 모듈부터 새 위치로 옮기고, 옛 경로는 그 Phase 안에서 제거한다.

### 3.2 렌더 파이프라인

```
RenderRequest ─▶ plan()          어느 레벨/원본을 읽을지, y범위, exact 여부 결정 (RenderPlan 값 객체)
              ─▶ fetch()         PyramidStore | RawStore 에서 BucketStats 슬라이스
              ─▶ project()       레이어 종류별 투영 (M4 / OHLC / envelope / bins / density)
              ─▶ paint()         Painter가 픽셀 버퍼에 그림
              ─▶ encode()        PNG / WebP / SVG
CachedTileRenderer(데코레이터)가 위 전체를 감싸 L1→L2 캐시 조회·저장
```

### 3.3 설계 패턴 적용 지점

| 패턴 | 적용 대상 | 대체하는 현재 코드 |
|---|---|---|
| Value Object (불변) | `PixelLattice`, `TileAddress`, `XRange`, `RenderPlan`, `YRange` | 흩어진 튜플·float 계산 (D9) |
| Monoid | `BucketStats.merge` | 피라미드 레벨별 재선택 로직 |
| Strategy | `Projection`(레이어별 투영), `Painter`, `YRangePolicy`, `TileEncoder` | `_draw_series` if/elif (D10) |
| Registry | `LayerKind → (Projection, Painter)` 등록표 | 차트 타입 분기 |
| Repository | `DatasetRepository`(meta), `RawStore`, `PyramidStore` | `access.py`의 전역 함수 + 경로 조립 |
| Decorator | `CachedTileRenderer` | `render_tile` 안에 섞인 캐시 코드 |
| Command + 디스패치 테이블 | WS 메시지 타입 → 핸들러 | `handle_ws_session`의 if/elif |
| Pipeline | plan → fetch → project → paint → encode | 단계가 섞인 `render_tile` |
| Adapter | 인제스트 커넥터 (`IngestBackend`) | 이미 적용됨, 유지 |

과설계를 막기 위한 규칙: **구현체가 2개 이상이 되거나, 테스트에서 대체가 필요할 때만** 추상화(Protocol)를 만든다. 그 외에는 함수 하나로 충분하다.

---

## 4. API 설계 — "원하는 대로, 어디서든"

### 4.1 ChartSpec v2: 레이어 + 축

```jsonc
{
  "dataset_id": "…",
  "x": { "column": "ts", "kind": "time", "timezone": "Asia/Seoul" },
  "axes": {
    "y":  { "label": "가격", "range_policy": "viewport" },
    "y2": { "label": "거래량", "scale": "log", "range_policy": "global" }
  },
  "layers": [
    { "kind": "candlestick", "column": "price", "axis": "y" },            // 원시 틱 → OHLC
    { "kind": "line", "column": "ma20", "axis": "y", "style": { "color": "#e45" } },
    { "kind": "bar",  "column": "volume", "agg": "sum", "axis": "y2" }
  ],
  "gaps": "break",
  "width": 1200, "height": 600, "format": "png"
}
```

- 이중 y축(`y`, `y2`)과 레이어별 차트 종류를 섞는 복합 차트를 지원한다.
- Candlestick은 두 가지 입력을 모두 받는다.
  - 원시 가격 컬럼 1개 → 집계로 OHLC를 생성한다.
  - 이미 만들어진 `open/high/low/close` 컬럼 → `first(open)`, `max(high)`, `min(low)`, `last(close)`로 병합한다.
- **v1 호환:** 기존 `y_columns`, `series`, `chart_type`은 모델 검증기에서 `layers`로 변환한다(어댑터). Phase 2 종료 시 deprecated로 표기한다.

### 4.2 전송 방식

| 용도 | 엔드포인트 | 특징 |
|---|---|---|
| 한 번 그리기 | `POST /api/v1/charts/render` | 기존 유지. 응답 헤더에 `X-Webchart-Exact`, `X-Webchart-Source` 추가 |
| 차트 등록 | `POST /api/v1/charts` → `chart_id` | spec의 내용 해시를 ID로 쓴다(같은 spec = 같은 ID) |
| **어디서든 임베드** | `GET /api/v1/charts/{chart_id}/image.png?x0=&x1=&w=&h=` | `<img src>` 한 줄. ETag/Cache-Control 지원. JS가 없는 환경, 리포트, 메일, 노트북 |
| HTTP 타일 | `GET /api/v1/charts/{chart_id}/tiles/{z}/{i}@{dpr}x.png` | XYZ 방식. CDN, 리버스 프록시 캐싱 가능 |
| 인터랙션 | `WS /api/v1/ws/{session_id}` | 스트리밍 (§4.3) |
| 정확 통계 | `POST /api/v1/charts/{chart_id}/query` | 구간 min/max/mean/count, 최근접 원본 점 |

### 4.3 WebSocket 프로토콜 v2

**클라이언트 → 서버**

| type | 필드 | 서버 동작 |
|---|---|---|
| `viewport` | `req_id, x_range, width_px, height_px, dpr` | 진행 중인 이전 렌더 **취소**(latest-wins) → 새 프레임 시작 |
| `hover` | `req_id, pixel_x, pixel_y` | 오래된 hover는 버린다. 모든 시리즈의 최근접 **원본** 점을 반환 |
| `select` | `req_id, x_range` | 구간 정확 통계 |
| `ping` | | `pong` |

**서버 → 클라이언트**

| type | 내용 |
|---|---|
| `frame_begin` | `req_id`, 타일 목록, 확정된 y범위(축별), **축 눈금(값·라벨·픽셀 위치)**, `exact`, `source` |
| *(binary)* | 고정 헤더 `[magic u8][ver u8][req_id u32][z u8][i u32][dpr u8][fmt u8]` + 이미지. **보이는 타일부터, 준비되는 대로** 전송 |
| `frame_end` | `req_id`, 전송 수, 캐시 적중 수, 소요 ms |
| `crosshair` | 시리즈별 `{x, y, label}` (원본 값) |
| `range_stats` | 시리즈별 `{min, max, mean, count}` |
| `error` | `req_id`, 코드, 메시지 |

축 눈금은 **서버가 계산**한다. 클라이언트 구현 언어와 상관없이 같은 눈금이 나오고, 시간축 nice tick과 타임존 처리도 한 곳에 모인다.

---

## 5. 경량 레퍼런스 클라이언트

`src/webchart/web/webchart.js`: 프레임워크 없는 ES 모듈 하나 (13.6KB). 서버의 `/viewer/`가 데모 페이지와 함께 내준다.

```
<div class="webchart">          ← 컨테이너
  <canvas data-layer="tiles">   ← 타일 합성 (ImageBitmap drawImage)
  <canvas data-layer="overlay"> ← 축, 눈금, 크로스헤어, 툴팁
</div>                          = DOM 3개, 데이터 크기와 무관
```

- 타일 디코딩은 `createImageBitmap`으로 메인 스레드 밖에서 한다. 디코딩된 비트맵은 LRU로 보관한다.
- 패닝과 줌 중에는 기존 타일을 변환해 즉시 보여주고, 서버의 정확한 타일이 도착하면 교체한다.
  - 전환 중 임시 화면은 근사라는 점을 문서에 명시한다. **확정 프레임은 항상 정확하다.**
- 마우스 이벤트는 `requestAnimationFrame`으로 합쳐 초당 최대 60회만 전송한다.
- 서버의 `/viewer` 경로에서 데모 페이지를 제공한다. 오라클 검증 결과를 눈으로 확인하는 용도도 겸한다.

---

## 6. 코드 컨벤션

이 프로젝트는 직접 작성하던 코드다. 사람이 읽고 유지보수하기 쉬운 것을 최우선으로 한다.

**이름**
- 단위를 이름에 넣는다: `width_px`, `x_ns`(epoch 나노초), `bucket_width`(데이터 단위), `ttl_s`
- 범위는 **반열린 구간 `[start, end)`** 로 통일하고 `XRange(start, end)` 값 객체로 다룬다. 닫힌 구간이 필요하면 이름에 드러낸다(`inclusive_end`).
- 불리언은 `is_`, `has_`, `should_`로 시작한다. 함수는 동사로 시작한다. 모듈은 명사로 짓는다.
- 축약어는 업계 관용어(`df`, `ddf`, `dpr`, `ws`, `px`)만 허용한다. `tmp`, `res`, `val` 같은 이름은 쓰지 않는다.

**주석과 docstring** — 한국어로 쓴다(용어는 원어 병기). 식별자는 영어로 쓴다.
- docstring에는 **무엇을, 왜, 불변식, 단위**를 적는다. 구현을 그대로 다시 말하는 주석은 쓰지 않는다.
- 무왜곡과 관련된 결정에는 근거를 적는다(예: `# M4 정리: 버킷 경계 = 픽셀 경계일 때만 정확`).
- 개선 작업으로 손대는 모듈은 기존 영어 주석도 한국어로 정리한다.

**타입과 구조**
- 모든 공개 함수에 타입 힌트를 단다. `domain/`은 `mypy --strict`를 통과해야 한다.
- 값 객체는 `@dataclass(frozen=True, slots=True)`로 만든다. API 경계에서만 Pydantic 모델을 쓴다.
- 전역 가변 상태는 lifespan에서 만드는 객체로 모으고 DI로 주입한다(`dependencies.py`).

**테스트**
- `domain/`: 단위 테스트 + 속성 테스트(예: "merge는 결합법칙을 만족한다", "M4 결과 그림 = 원본 그림")
- **오라클 테스트**(`tests/oracle/`): 합성 데이터(스파이크, 결측, 불규칙 간격, 다중 시리즈, 시간축)로 원본 렌더와 파이프라인 렌더의 픽셀 차이를 측정한다.
- 벤치마크(`scripts/bench/`)는 CI가 아니라 수동으로 실행하고, 결과를 문서에 기록한다.

**커밋**: 기존과 같은 Conventional Commits(`feat(scope): …`). 하나의 커밋은 하나의 논리적 변경이며, 테스트를 통과한 상태로 커밋한다.

---

## 7. 단계별 실행 계획

각 Phase는 **완료 기준을 만족해야** 다음으로 넘어간다.

### Phase 0 — 안전망과 기반 (기존 동작 유지)

| # | 작업 | 산출물 |
|---|---|---|
| 0.1 ✅ | 이 계획 문서와 CLAUDE.md 컨벤션 | `docs/improvement-plan.md` |
| 0.2 ✅ | **무왜곡 오라클 테스트 하네스**. 현재 실패하는 D1~D5는 `xfail(reason="D2")`로 기록 | `tests/oracle/` |
| 0.3 ✅ | 합성 데이터 생성기 + 벤치마크 스크립트 (N행, k시리즈, 스파이크, 결측, 시간축) | `scripts/bench/` |
| 0.4 ✅ | `domain/geometry.py`: `XRange`, `PixelLattice`, `TileAddress`. `math_utils`와 흩어진 좌표 계산을 통합 (D9) | 좌표 변환이 한 곳에 모임 |
| 0.5 ✅ | x 정규화: 시간 컬럼 파싱 → `timestamp[ns, UTC]`, meta에 `x_kind` 기록, 통계는 정규 수치로 저장 (D3) | 시계열 타일 동작 |

**완료 기준:** 기존 테스트와 오라클 하네스가 실행되고, 시간축 데이터셋으로 타일 세션이 동작한다.

### Phase 1 — 무왜곡 집계 코어

| # | 작업 |
|---|---|
| 1.1 ✅ | `domain/bucket_stats.py`: BucketStats 모노이드 + 정렬된 x에서 O(n) 벡터화 커널 (`searchsorted` + `reduceat`) |
| 1.2 ✅ | `RawStore`: x 정렬 인제스트 불변식, 로우그룹 zone map, pyarrow 범위 읽기. `read_window`의 전체 스캔 제거 (D6) |
| 1.3 ✅ | `PyramidStore` v2: 격자 정렬 레벨, 모든 시리즈, Arrow IPC mmap, 상위 레벨은 merge로 생성. meta에 `pyramid_version=2` 기록. v1 파일은 무시 |
| 1.4 ✅ | 투영: M4(line), envelope(band). 결측 버킷은 선 끊기 (P5) |
| 1.5 ✅ | 타일 경로 교체: 레벨 = z + t + log2(dpr), 깊은 줌은 원본 M4, 경계 이웃 버킷 포함. LTTB 재적용 제거 (D2, D5) |
| 1.6 ✅ | y범위 정책 + 캐시 키(`data_version`, layer hash, y범위, z, i, dpr, fmt) (D1) |
| 1.7 ✅ | 정적 렌더도 같은 파이프라인 사용. `exact`/`source` 응답 헤더 (P3) |

**완료 기준:** line/band 오라클 테스트가 픽셀 일치(안티앨리어싱 허용 오차 ε 이내)하고, D1·D2·D4·D5·D6의 xfail이 해제된다. 벤치마크 수치를 이 문서에 기록한다.

LTTB는 "빠른 미리보기용 근사" 옵션으로만 남기고 기본값에서 뺀다.

### Phase 2 — 차트 표현력 (Strategy + Registry)

| # | 작업 |
|---|---|
| 2.1 ✅ | ChartSpec v2 (layers, axes, gaps) + v1 → v2 어댑터 |
| 2.2 ✅ | Projection·Painter 레지스트리: line, area, bar(agg 명시), **candlestick** (D7, D10) |
| 2.3 ✅ | 이중 y축: 축별 y범위 정책, 축별 타일 키 |
| 2.4 ✅ | **밀도 래스터**: scatter, heatmap 2D 카운트 피라미드 + numpy 직접 페인터 (D7) |
| 2.5 ✅ | 축·눈금 서비스: nice number, 시간축 tick, 타임존. 정적 렌더와 WS가 공용으로 사용 |

**완료 기준:** 모든 레이어 종류의 오라클 테스트가 통과하고, v1 spec 요청이 그대로 동작한다.

### Phase 3 — 스트리밍 인터랙션과 경량 클라이언트

| # | 작업 |
|---|---|
| 3.1 ✅ | WS 프로토콜 v2: `req_id`, latest-wins 취소, 준비되는 대로 순차 전송, 바이너리 헤더 v2, 디스패치 테이블 (D8) |
| 3.2 ✅ | 인터랙션 질의: 다중 시리즈 crosshair(원본 값), 구간 통계 |
| 3.3 ✅ | 레퍼런스 웹 뷰어(DOM 3개) + `/viewer` 데모 |
| 3.4 ✅ | 차트 등록, 임베드 이미지 URL, HTTP XYZ 타일 (ETag) |

**완료 기준:** 1억 행 데이터셋에서 데모 뷰어로 빠르게 패닝·줌을 반복해도 요청이 쌓이지 않고, 뷰어의 DOM 노드가 3개다.

### Phase 4 — 대용량 RAM 활용과 운영

| # | 작업 |
|---|---|
| 4.1 ✅ | 메모리 예산 설정, L1 LRU, mmap 피라미드 관리, 메트릭(캐시 적중률, 렌더 지연, 경로별 exact 비율) |
| 4.2 ✅ | 10억 행 벤치마크와 튜닝. matplotlib이 GIL에 묶이는지 측정하고, 필요하면 페인트 단계를 프로세스 풀로 분리 |
| 4.3 ✅ | 실시간 append 인제스트: BucketStats 증분 merge → 영향받는 타일만 무효화 → WS로 갱신 푸시 |

### 보류

- **Dask 분산 클러스터:** 배포 대상이 RAM이 큰 단일 서버라 보류한다.
- **GPU 렌더링:** 밀도 래스터를 numpy로 구현한 뒤 측정하고, 병목일 때만 검토한다.

---

## 8. "왜곡 없음"의 정의 (오라클이 검사하는 것)

| 레이어 | 정의 | 허용 오차 |
|---|---|---|
| line | 원본 전체 래스터 = 집계 래스터 (픽셀 열별 잉크 외곽선 비교) | 세로 1px, 가로 1열 × dpr (아래 주) |
| band / candlestick | 픽셀 열별 min·max(·first·last)가 원본과 일치 | 0 |
| bar | 각 막대 값 = 명시한 집계(sum/mean/count)의 정확값 | 부동소수 오차 |
| density | 픽셀별 카운트가 원본 비닝과 일치 | 0 |
| hover / 통계 | 반환 값이 원본 행에 실제로 존재하는 값 | 0 |
| 결측 | 결측 구간을 선이 가로지르지 않음 (`gaps=break`) | 0 |

> 주: 가로 1열 허용은 **선 굵기 번짐** 때문이다 (Phase 1에서 실측).
> M4 정리는 폭이 0인 선에 대한 정리다. matplotlib은 선을 폭 있는 다각형으로 칠하므로,
> 픽셀 열 경계 근처의 가파른 선분은 이웃 열까지 번진다. 원본은 열마다 점이 수백 개라
> 번지는 모양이 M4(열당 4점)와 다르다. 실측: 원본 10만 점, 400px에서 허용 0열이면 어긋난 열 36%(최대 58px),
> 허용 1열이면 0열. 선 굵기 3px까지 같다. dpr 2에서는 선의 픽셀 굵기가 2배라 2열을 허용한다.
> 사라진 피크는 자기 열과 이웃 열 모두에서 수백 px 차이가 나므로 이 허용에 가려지지 않는다
> (기존 버그 7건 모두 허용 1열에서도 23~98% 열이 어긋났다).

---

## 9. 리스크와 결정 필요 사항

| 항목 | 내용 | 제안 |
|---|---|---|
| 정렬 비용 | 정렬되지 않은 수백 GB 소스는 외부 정렬이 필요해 인제스트가 느려진다 | 구현됨: 먼저 그대로 쓰고 정렬 컬럼 하나만 읽어 검사, 미정렬일 때만 Dask `sort_values` |
| matplotlib 처리량 | GIL 때문에 스레드 풀 확장에 한계가 있을 수 있다 | 측정함(Phase 4.2): 스레드 8 = 1개와 같고 프로세스 8 = 약 6배. 그리기 단계만 프로세스 풀로 분리했다 |
| 호환성 깨짐 | WS 프로토콜 v2, 피라미드 v2 | 실행함(Phase 3): 공식 클라이언트가 없어 WS v1을 제거하고 v2로 바꿨다. 피라미드 v1 파일은 무시하고 다시 빌드해야 한다. 되돌려야 하면 v1 메시지를 v2 핸들러로 잇는 어댑터를 디스패치 테이블에 추가하면 된다 |
| 기본 타임존 | 시간축 표시 기준 | 결정(Phase 2.5): ① 요청의 `x_axis.timezone` ② 없으면 컬럼의 원래 시간대 ③ naive 컬럼은 저장된 벽시계 그대로. CSV 오프셋 문자열은 UTC로 저장되므로 원래 오프셋을 보려면 ①을 쓴다 (다르게 원하면 변경 가능) |
| 피라미드 저장 공간 | 시리즈 수 × 레벨 | 메모리 예산 기반 자동 최대 레벨 + 시리즈 선택 빌드 |

---

## 부록 A. 용어

| 용어 | 뜻 |
|---|---|
| 격자 (lattice) | x 전체 범위를 2^L 균등 분할한 버킷 경계 집합. 피라미드, 타일, 픽셀이 공유한다 |
| 레벨 L | 격자 해상도. 버킷 수 = 2^L |
| 타일 (z, i) | 줌 z에서 x 전체를 2^z로 나눈 i번째 구간. T·dpr 픽셀 폭 |
| M4 | 픽셀 열별 first/last/min/max 네 점 집계 |
| 오라클 테스트 | 원본 전체 렌더를 정답으로 놓고 결과를 픽셀 비교하는 테스트 |
| exact | 응답 그림이 원본 전체 렌더와 일치함을 서버가 보증하는지 여부 |

---

## 10. 진행 기록

### Phase 0 — 완료 (2026-11-02)

| 커밋 | 내용 |
|---|---|
| `refactor(domain)` | `domain/geometry.py`: `XRange`, `TileAddress`, `PixelLattice`, `ScreenTransform`. `core/math_utils.py` 제거 |
| `test(oracle)` | `webchart.testing`(합성 데이터, 래스터 비교) + `tests/oracle/`. 알려진 버그를 strict xfail로 등록 |
| `feat(x-domain)` | 시간 컬럼 파싱, `ColumnInfo.kind/min_numeric/max_numeric/timezone`, 정규 좌표 경로 |
| `feat(bench)` | `scripts/bench/bench_pipeline.py` |

**오라클 현황** (`pytest tests/oracle`): 통과 5 (하네스 자체 검증 1 + 시간축 4), 알려진 실패 7

| 테스트 | 버그 | 측정된 어긋남 (픽셀 열) |
|---|---|---|
| 단일 시리즈 스파이크 | D4 | 316/512 (61.7%), 최대 439px |
| 단일 시리즈 노이즈 밴드 | D4 | 502/512 (98.0%), 최대 41px |
| 불규칙 간격 | D4 | 266/512 (52.0%), 최대 413px |
| 2번째 시리즈 피크 (타일) | D2 | 512/512 (100%), 최대 227px |
| 피라미드 타일의 시리즈별 envelope | D5 | 313/512 (61.1%), 최대 238px |
| 인접 타일 y 스케일 (줌 1) | D1 | 514/1024 (50.2%), 최대 230px |
| 2번째 시리즈 피크 (정적 렌더) | D2 | 축 라벨 폭이 달라져 그림 크기부터 다름 |

**벤치마크 기준선** (16코어, RAM 125GB, Redis 캐시 끔, `PYTHONPATH=src python scripts/bench/bench_pipeline.py`)

| 단계 | 1M행 × 2시리즈 | 20M행 × 2시리즈 |
|---|---:|---:|
| 인제스트 | 0.17s | 4.44s (RSS 2.4GB) |
| 타일 z=0, 피라미드 없음 | 0.17s | **실패** (행 수 상한 5M 초과) |
| 피라미드 빌드 | 0.93s | 4.00s (RSS 4.1GB) |
| 타일 z=0, 피라미드 | 0.03s | 0.05s |
| 깊은 줌 타일 (원본, 약 1만 행 필요) | 0.10s | **1.28s** |
| 정적 렌더 전체 범위 | 0.18s | **실패** (행 수 상한 5M 초과) |

관찰
- 깊은 줌 타일은 필요한 행이 약 1만 개뿐인데도 시간이 **전체 데이터 크기에 비례**한다(D6 실측).
  20억 행이면 타일 1장에 약 2분이 걸린다는 뜻이다. Phase 1.2(RawStore)의 1차 목표다.
- 정적 렌더는 피라미드를 쓰지 않아 5M행을 넘으면 전체 범위를 그릴 수 없다. Phase 1.7에서 같은 파이프라인으로 통합해 해결한다.

### Phase 1 — 완료 (2026-11-02)

| 커밋 | 내용 |
|---|---|
| `feat(domain)` | `BucketGrid`, `BucketStats` 모노이드(벡터 커널), `m4_polyline`(결측 끊기), `YRange`(matplotlib과 같은 자동 스케일). 오라클에 선 굵기 허용(8장 주) |
| `feat(raw-store)` | x 정렬 인제스트(131,072행 로우그룹), `data_version`/`sort_column`, zone map 범위 읽기 |
| `feat(render)` | 피라미드 v2(Arrow IPC + mmap), `application/`(windows·tiles·static_chart·y_ranges), 프레임 공유 y범위, 캐시 키 v2, `X-Webchart-Exact`/`Source` 헤더 |

구조 (계획 3.1의 계층이 생김)
- `domain/`: geometry, bucket_stats, projection, y_range. `mypy --strict` 통과
- `infrastructure/`: raw_store(zone map 범위 읽기·스트리밍 집계), pyramid_store(빌드·mmap 읽기)
- `application/`: windows(경로 선택 = plan), tiles(파이프라인 + `CachedTileRenderer` 데코레이터), static_chart, y_ranges, pyramids
- 제거: `services/render/tile_renderer.py`, `services/data/pyramid.py`(v1), `cache/keys.py`

경로 선택(`application/windows.py`)은 2.4의 계획대로다. 단, 원본 경로는 행을 모아 두지 않고
**로우그룹을 하나씩 읽어 집계**하므로, 범위가 아무리 넓어도 메모리가 터지지 않는다(이전의 500만 행 상한 실패가 사라짐).

**오라클 현황**: 18개 모두 통과, 알려진 실패 0. 기존 7건(D1·D2·D4·D5)이 해제됐고 다음을 새로 검사한다.
- 결측 구간 끊기 (원본·피라미드)
- 피라미드 최대 레벨보다 깊은 줌(원본 경로)의 타일 경계 연결
- dpr 2
- 시간축 피라미드
- 정적 렌더: 원본 경로(exact), 피라미드 근사(exact=false, 최대 3px 이내), 레벨과 경계가 맞는 피라미드(exact)

**벤치마크** (같은 장비, 캐시 없이 처음 그리기, `scripts/bench/bench_pipeline.py`)

| 단계 | 20M행 × 2시리즈 Phase 0 | Phase 1 |
|---|---:|---:|
| 인제스트 | 4.44s | 6.49s (정렬 검사 + 로우그룹) |
| 타일 z=0, 피라미드 없음 | **실패** (5M 상한) | 1.14s, exact |
| 정적 렌더 전체 범위 | **실패** (5M 상한) | 1.16s, exact (원본 2,000만 행 집계) |
| 피라미드 빌드 | 4.00s | 2.25s (L0..L20, 420MB) |
| 타일 z=0, 피라미드 | 0.05s | 0.03s, exact |
| 깊은 줌 타일 (원본) | 1.28s | **0.02s** (64배) |

1M행에서는 모든 단계가 0.6s 이하다. 깊은 줌 타일 시간은 이제 전체 데이터 크기와 무관하다(D6).

### Phase 2 — 완료 (2026-11-02)

**ChartSpec v2** (`models/chart.py`)
- `layers`: 종류는 line, area, band, bar, candlestick, scatter, heatmap
- `axes`: y, y2. 축마다 label, range, range_policy, log_scale
- `x_axis.timezone`
- v1 필드(`chart_type`, `y_columns`, `series`, `y_axis`)는 검증기가 레이어로 바꾼다. 이후 코드는 레이어만 본다.

**레지스트리** (D10 해소)
- `application/layers.py`의 `PROJECTIONS`: 레이어 종류 → 투영
- `services/render/painters.py`의 `PAINTERS`: 도형 종류 → 페인터
- 차트 종류를 추가할 때 렌더 경로의 다른 코드는 바꾸지 않는다.

**새 투영** (`domain/projection.py`)
- `envelope_band`
- `aggregate_bars`: sum, mean, count, min, max
- `price_candles`, `ohlc_candles`
- `DensityGrid`와 정규화(linear, log, eq_hist)
- 막대·캔들은 픽셀 열을 2의 거듭제곱 개씩(`group_px × dpr`) 묶는다. 그래서 타일 경계에서 잘리지 않고 값이 정확하다.

**이중 y축**
- 축마다 y범위 정책이 따로 적용된다. `bar`가 있는 축의 GLOBAL은 VIEWPORT로 계산한다(막대 값이 해상도에 따라 다르다).
- 타일 캐시 키에 축별 범위가 모두 들어간다.
- 그리는 순서는 y축 레이어가 위, y2가 아래다.

**밀도 래스터**
- 피라미드 빌드 때 2D 개수 레벨을 같이 만든다(x 최대 2^12열 × y 2048행, 시리즈당 레벨 합 약 64MB).
- y 범위는 GLOBAL 자동 범위와 같다. 그래서 GLOBAL 정책 타일은 피라미드에서 정확하게 나온다(행 2048 = 512 × dpr 4의 배수).
- 그 외에는 원본을 로우그룹 단위로 센다.

**눈금 서비스** (`domain/ticks.py`, `application/axes.py`)
- 수치는 1·2·5 × 10^k 간격, 시간은 ms~년 단위다. 표시 시간대의 벽시계에 맞춰 정렬한다(서머타임 포함).
- 정적 렌더가 이 눈금을 쓴다. Phase 3의 WS `frame_begin`도 같은 함수를 쓴다.

**수정한 버그** (오라클이 찾음): 피라미드 셀을 픽셀로 다시 묶을 때 두 가지 문제가 있었다.
- 셀 *시작* 좌표가 경계에 걸려 앞 칸에 들어갔다.
- 격자 밖 셀을 끝 칸에 잘라 넣었다.

셀 *중심*으로 배정하고 격자 밖 셀은 빼도록 고쳤다.

**오라클**: 33개 모두 통과한다. 새 레이어 오라클(`tests/oracle/test_layer_oracle.py`, 15개)은 다음을 원본·피라미드 경로 모두에서 검사한다.
- band: 열별 min/max
- bar: 집계 5종 각각의 값
- candlestick: 가격 컬럼, OHLC 컬럼
- 밀도: 픽셀별 개수, dpr 1·2
- 타일 이음새(4장을 이은 그림 = 한 장)
- 이중 축 범위

### Phase 3 — 완료 (2026-11-02)

**WS 프로토콜 v2** (`services/session/stream.py`, `models/ws_messages.py`, D8 해소)
- 디스패치 테이블과 타입별 요청: viewport, hover, select, ping
- viewport·hover·select는 작업(Task)으로 돈다. 같은 종류의 새 요청이 오면 이전 작업을 취소한다.
- 타일 렌더는 연결별 세마포어를 기다린 뒤에야 렌더 풀에 들어간다. 그래서 취소된 프레임은 풀을 막지 않는다.
- 보이는 타일부터 다 그려진 순서대로 보낸다.
- 바이너리 헤더는 13바이트다.
- `frame_begin`에 축별 y범위와 서버가 계산한 x/y 눈금이 들어간다.

**인터랙션 질의** (`application/interaction.py`)
- 크로스헤어: 시리즈마다 결측이 아닌 가장 가까운 *원본 행*을 찾고, 표시 시간대 라벨을 붙인다.
- 구간 통계: 구간 안에 완전히 들어가는 피라미드 버킷은 merge하고, 양 끝 자투리만 원본에서 읽는다. 넓은 구간도 빠르고 결과는 정확하다.

**차트 등록·임베드** (`application/chart_registry.py`, `api/v1/charts.py`)
- `POST /charts`: spec 내용 해시가 chart_id다.
- `GET /charts/{id}/image.png?x0&x1&w&h`, `GET /charts/{id}/tiles/{z}/{i}@{dpr}x.png`
  - ETag/304, `Cache-Control`
  - 인증이 켜져 있으면 차트별 HMAC 토큰을 URL에 싣는다. `<img>`·CDN은 헤더를 못 붙이고, 키 자체는 URL에 노출하지 않는다.

**레퍼런스 뷰어** (`/viewer/`): DOM은 컨테이너와 캔버스 2개로 3개다.
- 줌은 타일 레벨(2배)에 맞춰 바꾼다. 그래서 확정 프레임은 타일 1px = 화면 1px이다.
- 이동·줌 도중에는 받아 둔 타일을 옮겨 바로 보여 준다.
- 타일은 `createImageBitmap`으로 디코딩하고 LRU 256장을 보관한다.
- 입력은 rAF로 모아 보낸다.
- 크로스헤어 툴팁, Shift+드래그 구간 통계

E2E 확인 (Playwright + Chrome, 500만 행 시간축, 캐시 없이 처음 그리기)
- 휠 줌 4단계, 드래그 이동, 호버, 구간 선택에서 콘솔 오류가 없었다.
- 차트 DOM 노드는 3개였다.
- 프레임(타일 7장)은 약 0.6초였다.

### Phase 4 — 완료 (2026-11-02)

**4.1 메모리·지표**
- L1 타일 캐시: 프로세스 메모리, 바이트 상한 LRU (`tile_memory_cache_bytes`, 기본 512MB). Redis(L2) 앞에서 같은 워커의 재요청을 받는다.
- `/metrics` (Prometheus 텍스트)
  - 타일 요청: l1_hit / l2_hit / rendered
  - 경로별 렌더 지연 히스토그램
  - 경로별·exact별 렌더 수
  - 정적 렌더
- 피라미드 파일은 `.npy` mmap이다. 열린 배열은 inode 기준으로 캐시하므로, 실시간 추가로 고친 파일도 공유 매핑으로 바로 보인다.

**4.2 측정과 튜닝**

그리기 병렬성 (`scripts/bench/bench_render_parallelism.py`, 500만 행, 줌 5 타일 32장)

| 방식 | 타일/초 |
|---|---:|
| 1 스레드 | 62 |
| 스레드 8 | 53 (GIL 때문에 늘지 않는다) |
| 프로세스 8 | 390 |

그래서 그리기만 spawn 프로세스 풀로 보낸다 (`paint_processes`). fetch·project는 GIL을 대부분 놓아 스레드에 둔다.

이 과정에서 찾은 두 문제
- 그리기 워커가 application 계층의 객체를 unpickle하면서 Dask까지 import해 워커마다 첫 요청이 0.4초 늦었다. 그리기용 단순 타입(`PaintItem`)으로 바꿔 보내도록 고쳤다.
- 피라미드 빌드의 2D 밀도 누적이 로우그룹마다 격자 전체(840만 셀) 크기의 배열을 새로 만들었다(1억 행 빌드 43초의 대부분). 칠해진 셀에만 더하도록 고쳤다 → 17.8초.

1억 행 × 2시리즈 (`bench_pipeline.py --rows 100000000`, 그리기 프로세스 8)

| 단계 | 시간 |
|---|---:|
| 인제스트 | 17.4s (최대 RSS 8GB) |
| 피라미드 빌드 | 17.8s (L0..L22, 1.9GB) |
| 타일 z=0 (피라미드) | 0.03s |
| 깊은 줌 타일 (원본) | 0.03s |
| 정적 렌더 전체 범위 (피라미드, 근사) | 0.11s |
| 정적 렌더 전체 범위 (피라미드 없음, 원본 스트리밍 집계, exact) | 5.6s |
| 타일 64장 동시 (정상 상태) | 0.28s |

Phase 3 완료 기준 확인 (1억 행, Playwright + Chrome)
- 휠 줌·드래그를 2.8초 동안 40번(viewport 72개) 보내자 중간 프레임은 모두 취소되었다.
- 마지막 입력 0.26초 뒤에 최종 프레임(타일 6장, exact)이 도착했다.
- DOM 노드는 3개, 콘솔 오류는 없었다.

10억 행은 측정하지 못했다. 장비의 남은 디스크(79GB, 다른 작업과 공유)로는 원본·스테이징·피라미드를 함께 둘 수 없다. 1억 행 결과로 미루어 보면 다음과 같다.
- 타일 경로(피라미드·깊은 줌)는 데이터 크기와 무관하다.
- 인제스트와 피라미드 빌드는 행 수에 비례한다(각각 약 3분 예상).

**4.3 실시간 추가** (`POST /datasets/{id}/rows`)
- 추가된 행은 새 정렬 Parquet 파일 하나로 쓴다. 피라미드는 다시 빌드하지 않고 모든 레벨에 제자리 merge한다.
- 격자는 축 컬럼별로 고정한다(`x_domains`). 행이 밖에 오면 2배씩 넓힌다.
  - 넓혀도 이전 레벨 L이 새 레벨 L+1의 절반과 경계가 같아서, 데이터를 다시 읽지 않고 파일만 옮겨 붙인다.
  - 넓힌 피라미드는 새 storage 디렉터리에 만들고 meta.json으로 한 번에 바꾼다.
- 추가마다 "그림이 바뀌는 x 구간"을 기록한다. 추가 행에 선이 이어지는 앞뒤 기존 행까지 넓힌 구간이다.
  - 타일 키에 그 타일을 바꾼 마지막 세대(tile epoch)를 넣는다. 닿지 않은 타일은 캐시를 그대로 쓴다.
- WS에는 `data_appended`를 보낸다. 뷰어는 바뀐 구간이 보일 때만 다시 요청하고, 격자가 넓어지면 줌 번호를 올려 화면 배율을 유지한다.

**타일 덧그리기**: 4.3 오라클이 찾은 문제다.
- 증상: 타일 경계에 가파른 선분이 걸리면 타일마다 따로 잘려, 이어 그린 그림과 1열이 2px 달랐다.
- 확인: 원본·피라미드·추가 경로 모두 같은 결과였다. 따라서 추가 기능의 문제가 아니라 타일링 자체의 문제다.
- 해결: 타일을 같은 레벨 격자로 양옆 4px씩 더 넓게 그린 뒤 잘라 낸다.
  - 선 레이어만 넓힌다. 막대·캔들·띠는 픽셀에 맞춘 채우기라 넓히지 않는다(묶음 경계 유지).
  - 오라클 정답도 같은 여백으로 그린다. 데이터 중간을 자른 띠는 비교할 때 끝의 reach 열을 뺀다(띠 밖 피크의 번짐을 판정할 이웃 열이 없으므로).

**오라클**: 37개 모두 통과한다. 실시간 추가 오라클은 끝 뒤 추가(격자 2회 확장)와 범위 안 추가를 다룬다. 결과는 둘 다 전체를 다시 그린 그림과 같았고, merge한 피라미드는 새로 빌드한 피라미드와 같았다.

### 남은 일 (다음 단계 후보)

- 워커가 여러 개일 때 실시간 추가 알림: 지금은 프로세스 안 발행·구독이다. Redis pub/sub으로 바꿔야 한다.
- 10억 행 벤치마크: 디스크 여유가 있는 장비에서 측정한다.
- 정적 렌더의 근사 경로: 정렬되지 않은 x범위는 픽셀 열 폭의 1/8 버킷으로 다시 묶는다. 경계 버킷을 원본으로 보정하면 정확하게 만들 수 있다(구간 통계에서 쓴 "피라미드 안쪽 + 원본 자투리" 방식).
- 밀도 피라미드는 GLOBAL y범위에서만 정렬된다. VIEWPORT 범위의 scatter/heatmap 타일은 원본을 센다(정확하지만 느리다).
