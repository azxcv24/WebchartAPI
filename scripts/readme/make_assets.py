"""README 이미지·움짤을 만든다 (docs/assets/).

모든 데이터는 여기서 만드는 합성 데이터다. 실행 중인 서버에 file:// 인제스트로 올리고,
그 서버의 API와 뷰어로 그린다. 그래서 이미지는 지금 코드가 실제로 그린 결과다.

준비
    docker run -d --rm -p 6379:6379 redis:7-alpine
    WEBCHART_DATA_DIR=/tmp/wc-data WEBCHART_LOCAL_INGEST_ROOT=/tmp/wc-ingest \\
        uvicorn webchart.main:app --port 8000
    pip install playwright && playwright install chromium     # 움짤 녹화용
    (ffmpeg가 PATH에 있어야 한다)

실행
    PYTHONPATH=src python scripts/readme/make_assets.py \\
        --server http://127.0.0.1:8000 --ingest-root /tmp/wc-ingest --rows 100000000

만드는 파일
    docs/assets/demo.webp         뷰어 녹화 (1억 행: 줌인, 이동, 호버, 구간 통계)
    docs/assets/distortion.png    원본 전체 / 평균 다운샘플링 / WebchartAPI 비교
    docs/assets/gallery.png       레이어 종류 모음
"""

import argparse
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "docs" / "assets"
START = pd.Timestamp("2026-01-01 00:00:00")
FONT_PATHS = [
    "/usr/share/fonts/truetype/nanum/NanumSquare_acB.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]


# --- 합성 데이터 ------------------------------------------------------------------


def sensor_frame(rows: int, step_ms: int, seed: int) -> tuple[pd.DataFrame, list[int]]:
    """센서처럼 보이는 시계열 2개. 하루 주기 + 느린 흔들림 + 잡음 + 단일 샘플 스파이크 + 결측.

    돌려주는 두 번째 값은 스파이크가 있는 행 번호다 (데모에서 그 위치로 줌인한다).
    """
    rng = np.random.default_rng(seed)
    ts = START.value + np.arange(rows, dtype=np.int64) * step_ms * 1_000_000
    day = (ts - ts[0]) / 86_400e9
    knots = rng.normal(0, 1, 4_000).cumsum()
    wander = np.interp(np.linspace(0, len(knots) - 1, rows), np.arange(len(knots)), knots)
    temp = 20 + 5 * np.sin(2 * np.pi * day) + 0.06 * wander + rng.normal(0, 0.25, rows)
    load = 55 + 25 * np.sin(2 * np.pi * day - 1.2) ** 3 + rng.normal(0, 3, rows)

    spikes = np.sort(rng.choice(np.arange(rows // 50, rows - rows // 50), 24, replace=False))
    temp[spikes] += rng.uniform(9, 16, len(spikes)) * rng.choice([1, 1, 1, -1], len(spikes))
    for start in rng.choice(np.arange(rows // 10, rows - rows // 10), 3, replace=False):
        temp[start : start + rows // 300] = np.nan  # 결측 구간: 선이 끊긴다
    frame = pd.DataFrame({"ts": ts.astype("datetime64[ns]"), "temp": temp, "load": load})
    return frame, spikes.tolist()


def market_frame(rows: int, seed: int) -> pd.DataFrame:
    """1초 간격 체결가(기하 랜덤워크)와 거래량."""
    rng = np.random.default_rng(seed)
    ts = START.value + np.arange(rows, dtype=np.int64) * 1_000_000_000
    price = 100 * np.exp(np.cumsum(rng.normal(0, 0.0004, rows)))
    volume = rng.lognormal(1.0, 0.8, rows)
    return pd.DataFrame({"ts": ts.astype("datetime64[ns]"), "price": price, "volume": volume})


def points_frame(rows: int, seed: int) -> pd.DataFrame:
    """밀도 지도용 점 구름: 굽은 띠 두 개와 덩어리 하나."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(0, 1000, rows)
    band = rng.integers(0, 3, rows)
    y = np.where(
        band == 0,
        3 * np.sin(x / 80) + rng.normal(0, 0.6, rows),
        np.where(band == 1, -2 + 0.004 * x + rng.normal(0, 1.4, rows), rng.normal(1.5, 0.5, rows)),
    )
    x = np.where(band == 2, rng.normal(620, 40, rows), x)
    return pd.DataFrame({"x": x, "y": y})


# --- 서버 ---------------------------------------------------------------------


class Server:
    def __init__(self, base_url: str, ingest_root: Path) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=600)
        self.ingest_root = ingest_root

    def ingest(self, name: str, frame: pd.DataFrame, pyramid_x: str | None) -> str:
        """file:// 인제스트 → (피라미드 빌드) → 데이터셋 ID."""
        path = self.ingest_root / f"{name}.parquet"
        frame.to_parquet(path, index=False)
        started = time.perf_counter()
        response = self.http.post(
            "/api/v1/datasets/connect", json={"protocol": "file", "uri": f"file://{path}"}
        )
        response.raise_for_status()
        dataset_id = response.json()["dataset_id"]
        self._wait(
            lambda: (
                self.http.get(f"/api/v1/datasets/{dataset_id}").json()["status"]
                in ("ready", "error")
            )
        )
        assert self.http.get(f"/api/v1/datasets/{dataset_id}").json()["status"] == "ready"
        print(f"  {name}: {len(frame):,}행 인제스트 {time.perf_counter() - started:.1f}s")
        if pyramid_x:
            started = time.perf_counter()
            self.http.post(f"/api/v1/datasets/{dataset_id}/pyramid", json={"x_column": pyramid_x})
            self._wait(
                lambda: pyramid_x in self.http.get(f"/api/v1/datasets/{dataset_id}/pyramids").json()
            )
            print(f"  {name}: 피라미드 {time.perf_counter() - started:.1f}s")
        path.unlink()
        return dataset_id

    def render(self, spec: dict) -> tuple[Image.Image, httpx.Response]:
        response = self.http.post("/api/v1/charts/render", json=spec)
        response.raise_for_status()
        return Image.open(BytesIO(response.content)).convert("RGB"), response

    @staticmethod
    def _wait(done, timeout_s: float = 900) -> None:
        deadline = time.monotonic() + timeout_s
        while not done():
            if time.monotonic() > deadline:
                raise TimeoutError("서버 작업이 끝나지 않았다")
            time.sleep(0.5)


# --- 이미지 조립 ------------------------------------------------------------------


def korean_count(rows: int) -> str:
    """1억, 2,000만 같은 읽기 쉬운 행 수."""
    if rows >= 100_000_000:
        return f"{rows / 100_000_000:g}억"
    return f"{rows / 10_000:,.0f}만"


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def captioned(image: Image.Image, title: str, note: str = "", accent: str = "#222") -> Image.Image:
    """이미지 위에 제목 줄을 붙인다."""
    header = 40 if not note else 62
    canvas = Image.new("RGB", (image.width, image.height + header), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((14, 10), title, font=font(19), fill=accent)
    if note:
        draw.text((14, 36), note, font=font(14), fill="#666")
    canvas.paste(image, (0, header))
    return canvas


def stack(images: list[Image.Image], columns: int, gap_px: int = 12) -> Image.Image:
    """같은 크기 칸에 격자로 놓는다 (칸 = 가장 큰 이미지, 남는 곳은 흰색)."""
    cell_w = max(im.width for im in images)
    cell_h = max(im.height for im in images)
    rows = (len(images) + columns - 1) // columns
    canvas = Image.new(
        "RGB",
        (cell_w * columns + gap_px * (columns - 1), cell_h * rows + gap_px * (rows - 1)),
        "#e8e8e8",
    )
    for index, im in enumerate(images):
        x = (index % columns) * (cell_w + gap_px)
        y = (index // columns) * (cell_h + gap_px)
        canvas.paste(Image.new("RGB", (cell_w, cell_h), "white"), (x, y))
        canvas.paste(im, (x, y))
    return canvas


# --- 그림 1: 왜곡 비교 --------------------------------------------------------------


def make_distortion(server: Server) -> None:
    sys.path.insert(0, str(REPO))
    from tests.oracle.harness import render_reference_chart  # 오라클과 같은 정답 렌더러

    from webchart.models.chart import ChartSpec
    from webchart.models.dataset import ColumnInfo
    from webchart.testing.raster import compare_envelopes, decode_png, ink_mask

    frame, _ = sensor_frame(5_000_000, step_ms=200, seed=7)
    dataset_id = server.ingest("compare", frame, pyramid_x=None)
    spec = {
        "dataset_id": dataset_id,
        "x_axis": {"column": "ts"},
        "layers": [{"kind": "line", "column": "temp", "style": {"line_width": 1.0}}],
        "width": 1000,
        "height": 260,
        "legend": False,
    }
    exact, exact_response = server.render(spec)
    average, _ = server.render({**spec, "downsample_algorithm": "average"})

    schema = server.http.get(f"/api/v1/datasets/{dataset_id}/schema").json()["columns"]
    x_info = ColumnInfo.model_validate(next(c for c in schema if c["name"] == "ts"))
    started = time.perf_counter()
    reference_png = render_reference_chart(ChartSpec.model_validate(spec), frame, x_info)
    reference_s = time.perf_counter() - started
    reference = Image.open(BytesIO(reference_png)).convert("RGB")
    diff = compare_envelopes(
        ink_mask(decode_png(reference_png)), ink_mask(decode_png(exact_response.content)), 1, 1
    )
    headers = exact_response.headers
    print(f"  비교: 원본과 다른 열 {diff.mismatched_columns}개")

    panels = [
        captioned(
            reference,
            "① 원본 500만 점을 전부 그림",
            f"matplotlib로 모든 점을 그린 정답 ({reference_s:.1f}초)",
        ),
        captioned(
            average,
            "② 흔한 다운샘플링 (구간 평균)",
            "스파이크가 평균에 묻혀 깎이거나 사라지고, 잡음 폭이 좁아진다",
            accent="#b3261e",
        ),
        captioned(
            exact,
            "③ WebchartAPI (M4 픽셀 정렬 집계)",
            f"①과 픽셀 단위로 같다 (다른 열 {diff.mismatched_columns}개, X-Webchart-Exact: "
            f"{headers['X-Webchart-Exact']})",
            accent="#1b6e3a",
        ),
    ]
    stack(panels, columns=1).save(OUT_DIR / "distortion.png", optimize=True)


# --- 그림 2: 레이어 모음 -----------------------------------------------------------


def make_gallery(server: Server, sensor_id: str, sensor: pd.DataFrame) -> None:
    market_id = server.ingest("market", market_frame(2_000_000, seed=3), pyramid_x="ts")
    points_id = server.ingest("points", points_frame(20_000_000, seed=5), pyramid_x="x")

    def days(first: float, last: float) -> list[float]:
        """시간축 범위 (정규 좌표 = epoch 나노초)."""
        return [float((START + pd.Timedelta(days=d)).value) for d in (first, last)]

    one_day, two_days, week = days(3, 4), days(5, 7), days(0, 2)
    size = {"width": 620, "height": 300, "legend": False}

    def spec(dataset_id: str, x: str, layers: list, x_range=None, **extra) -> dict:
        x_axis = {"column": x} | ({"range": x_range} if x_range else {})
        return {"dataset_id": dataset_id, "x_axis": x_axis, "layers": layers} | size | extra

    charts = [
        (
            "line",
            f"센서 {korean_count(len(sensor))} 행 전체",
            spec(sensor_id, "ts", [{"kind": "line", "column": "temp"}]),
        ),
        (
            "band",
            "픽셀 열별 최소~최대 (하루)",
            spec(sensor_id, "ts", [{"kind": "band", "column": "temp"}], one_day),
        ),
        (
            "line + area (y2)",
            "두 축 (이틀)",
            spec(
                sensor_id,
                "ts",
                [
                    {"kind": "line", "column": "temp"},
                    {
                        "kind": "area",
                        "column": "load",
                        "axis": "y2",
                        "style": {"opacity": 0.3, "color": "#e07b39"},
                    },
                ],
                two_days,
            ),
        ),
        (
            "candlestick + bar (y2)",
            "체결 200만 건 → 캔들 + 거래량 합",
            spec(
                market_id,
                "ts",
                [
                    {"kind": "candlestick", "column": "price", "group_px": 8},
                    {
                        "kind": "bar",
                        "column": "volume",
                        "agg": "sum",
                        "axis": "y2",
                        "group_px": 8,
                        "style": {"opacity": 0.3},
                    },
                ],
                week,
            ),
        ),
        (
            "bar",
            "구간 평균 (16px 단위)",
            spec(
                sensor_id,
                "ts",
                [{"kind": "bar", "column": "load", "agg": "mean", "group_px": 16}],
                two_days,
            ),
        ),
        (
            "heatmap",
            "점 2,000만 개의 2D 밀도 (eq_hist)",
            spec(
                points_id, "x", [{"kind": "heatmap", "column": "y", "style": {"colormap": "magma"}}]
            ),
        ),
    ]
    panels = []
    for kind, note, chart_spec in charts:
        image, response = server.render(chart_spec)
        flag = "exact" if response.headers["X-Webchart-Exact"] == "true" else "approx"
        panels.append(captioned(image, kind, f"{note} · {flag}"))
    stack(panels, columns=2).save(OUT_DIR / "gallery.png", optimize=True)


# --- 움짤: 뷰어 녹화 ---------------------------------------------------------------


def make_demo(server: Server, sensor_id: str, spike_x: float, rows: int) -> None:
    from playwright.sync_api import sync_playwright

    width_px = 1100
    with tempfile.TemporaryDirectory() as video_dir, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": width_px, "height": 700},
            record_video_dir=video_dir,
            record_video_size={"width": width_px, "height": 700},
        )
        page = context.new_page()
        # 녹화용으로만 차트 객체를 window에 노출한다 (스파이크의 화면 위치를 줌마다 읽으려고).
        # 저장소의 webchart.js는 바꾸지 않는다.
        script = (REPO / "src/webchart/web/webchart.js").read_text()
        script = script.replace("this.options = {", "window.__chart = this; this.options = {", 1)
        page.route(
            "**/webchart.js",
            lambda route: route.fulfill(body=script, content_type="text/javascript"),
        )
        page.goto(f"{server.http.base_url}viewer/")
        page.wait_for_function(
            f"[...document.querySelectorAll('#dataset option')]"
            f".some(o => o.value === '{sensor_id}')"
        )
        page.select_option("#dataset", sensor_id)
        page.wait_for_function("document.querySelector('#y-column').options.length > 0")
        page.select_option("#x-column", "ts")
        page.select_option("#y-column", "temp")
        page.select_option("#y-policy", "viewport")
        page.click("button[type=submit]")
        page.wait_for_function("document.querySelector('#status').textContent.includes('프레임')")
        page.wait_for_timeout(1500)

        chart = page.locator("#chart").bounding_box()
        status = page.locator("#status").bounding_box()
        canvas = page.locator("#chart canvas[data-layer=overlay]").bounding_box()
        y_mid = canvas["y"] + canvas["height"] * 0.45

        def spike_px() -> float:
            return canvas["x"] + page.evaluate(f"window.__chart.xToPx({spike_x})")

        for _ in range(16):  # 2배씩 16번: 11.6일 → 화면에 십여 초 (원본 점이 보인다)
            page.mouse.move(spike_px(), y_mid)  # 줌마다 스파이크 위로 커서를 다시 맞춘다
            page.mouse.wheel(0, -120)
            page.wait_for_timeout(420)
        at_spike = spike_px()
        page.wait_for_timeout(700)
        for step in range(0, 31):  # 호버: 커서에 가장 가까운 원본 값
            page.mouse.move(at_spike - 240 + step * 8, y_mid)
            page.wait_for_timeout(40)
        page.wait_for_timeout(900)
        # Shift+드래그: 스파이크를 포함한 구간의 정확한 통계 (max = 스파이크 값)
        page.keyboard.down("Shift")
        page.mouse.move(at_spike - 160, y_mid)
        page.mouse.down()
        for step in range(1, 21):
            page.mouse.move(at_spike - 160 + step * 14, y_mid)
            page.wait_for_timeout(35)
        page.mouse.up()
        page.keyboard.up("Shift")
        page.wait_for_timeout(2000)
        # 드래그로 이동
        page.mouse.move(chart["x"] + 800, y_mid + 80)
        page.mouse.down()
        for step in range(1, 21):
            page.mouse.move(chart["x"] + 800 - step * 20, y_mid + 80)
            page.wait_for_timeout(40)
        page.mouse.up()
        page.wait_for_timeout(900)
        # 다시 전체로
        page.mouse.move(at_spike, y_mid)
        for _ in range(16):
            page.mouse.wheel(0, 120)
            page.wait_for_timeout(150)
        page.wait_for_timeout(1500)
        video_path = page.video.path()
        context.close()
        browser.close()

        top = int(chart["y"]) - 4
        height = int(status["y"] + status["height"]) - top + 6
        crop = f"crop={int(chart['width']) + 8}:{height}:{int(chart['x']) - 4}:{top}"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                video_path,
                "-vf",
                f"{crop},fps=12,scale=880:-1:flags=lanczos",
                "-c:v",
                "libwebp",
                "-lossless",
                "0",
                "-q:v",
                "60",
                "-compression_level",
                "6",
                "-loop",
                "0",
                "-an",
                str(OUT_DIR / "demo.webp"),
            ],
            check=True,
        )
    print(f"  demo.webp {(OUT_DIR / 'demo.webp').stat().st_size / 1e6:.1f} MB ({rows:,}행)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--ingest-root", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=100_000_000)
    parser.add_argument("--only", choices=["distortion", "gallery", "demo"], action="append")
    parser.add_argument("--sensor-id", help="이미 올린 센서 데이터셋 ID (같은 --rows로 만든 것)")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    server = Server(args.server.rstrip("/") + "/", args.ingest_root)
    only = set(args.only or ["distortion", "gallery", "demo"])

    if "distortion" in only:
        make_distortion(server)
    if only & {"gallery", "demo"}:
        sensor, spikes = sensor_frame(args.rows, step_ms=10, seed=11)
        sensor_id = args.sensor_id or server.ingest("sensor", sensor, pyramid_x="ts")
        if "gallery" in only:
            make_gallery(server, sensor_id, sensor)
        if "demo" in only:
            # 가장 큰 스파이크 쪽으로 줌인한다
            target = max(spikes, key=lambda row: abs(sensor["temp"].iloc[row] - 20))
            make_demo(server, sensor_id, float(sensor["ts"].iloc[target].value), args.rows)


if __name__ == "__main__":
    main()
