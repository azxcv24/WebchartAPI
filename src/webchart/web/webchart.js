// WebchartAPI 경량 클라이언트 (계획 문서 5장). 프레임워크 없음, DOM 3개.
//
//   <div class="webchart">            컨테이너
//     <canvas data-layer="tiles">     서버가 그린 타일 합성 (drawImage)
//     <canvas data-layer="overlay">   축 눈금, 크로스헤어, 툴팁, 구간 선택
//
// 무왜곡: 줌은 타일 레벨(2배 단위)에 맞춰서만 바꾼다. 그래서 확정된 화면에서는 타일 픽셀
// 하나가 화면 CSS 픽셀 하나에 정확히 대응한다 (서버가 그 픽셀 열 그대로 집계한 그림).
// 이동·줌 도중에는 이미 받은 타일을 옮기거나 늘려 바로 보여 주고(근사), 서버의 새 타일이
// 도착하면 바꿔 그린다.

const HEADER_BYTES = 13;  // [magic u8][ver u8][req_id u32][zoom u8][index u32][dpr u8][fmt u8]
const BITMAP_CACHE_LIMIT = 256;

export class WebChart {
  /**
   * @param {HTMLElement} container
   * @param {{baseUrl?: string, sessionId: string, apiKey?: string,
   *          domain: [number, number], tileSize?: number, formatX?: (x:number)=>string}} options
   *   domain: x 전체 범위 (정규 좌표, 데이터셋 스키마의 min_numeric/max_numeric)
   */
  constructor(container, options) {
    this.options = { baseUrl: "", tileSize: 512, formatX: (x) => x.toPrecision(6), ...options };
    this.domain = { start: options.domain[0], end: options.domain[1] };
    this.zoom = 0;
    this.viewStart = this.domain.start;
    this.frameReqId = 0;          // viewport 요청 번호 (프레임 응답을 고를 때 쓴다)
    this.queryReqId = 0;          // hover·select 요청 번호
    this.frame = null;            // 마지막 frame_begin
    this.frameYKey = new Map();   // req_id → 그 프레임의 y범위 키 (타일 캐시 키 일부)
    this.bitmaps = new Map();     // `${yKey}|${z}|${i}|${dpr}` → ImageBitmap (LRU)
    this.crosshair = null;
    this.selection = null;        // {start, end, stats}
    this.pending = { viewport: false, hover: null };

    container.classList.add("webchart");
    container.style.position = "relative";
    this.tiles = this.#canvas(container, "tiles");
    this.overlay = this.#canvas(container, "overlay");
    this.#resize(container);
    this.fitAll();
    new ResizeObserver(() => { this.#resize(container); this.#requestViewport(); }).observe(container);
    this.#bindPointer();
    this.#connect();
  }

  // --- 화면 좌표 -------------------------------------------------------------

  get dpr() { return [4, 2, 1].find((d) => window.devicePixelRatio >= d) || 1; }
  get width() { return this.overlay.clientWidth; }
  get height() { return this.options.tileSize; }
  /** 현재 줌에서 CSS 1px이 덮는 x 폭 (타일 픽셀과 같다). */
  get xPerPx() { return (this.domain.end - this.domain.start) / (this.options.tileSize * 2 ** this.zoom); }
  get viewEnd() { return this.viewStart + this.width * this.xPerPx; }
  xToPx(x) { return (x - this.viewStart) / this.xPerPx; }
  pxToX(px) { return this.viewStart + px * this.xPerPx; }

  #canvas(container, layer) {
    const canvas = document.createElement("canvas");
    canvas.dataset.layer = layer;
    canvas.style.cssText = "position:absolute;left:0;top:0;width:100%;";
    container.appendChild(canvas);
    return canvas;
  }

  #resize(container) {
    container.style.height = `${this.height}px`;
    for (const canvas of [this.tiles, this.overlay]) {
      canvas.style.height = `${this.height}px`;
      canvas.width = Math.round(container.clientWidth * this.dpr);
      canvas.height = Math.round(this.height * this.dpr);
    }
    this.#draw();
  }

  // --- 서버 연결 -------------------------------------------------------------

  #connect() {
    const { baseUrl, sessionId, apiKey } = this.options;
    const origin = baseUrl || location.origin;
    const url = new URL(`/api/v1/ws/${sessionId}`, origin.replace(/^http/, "ws"));
    if (apiKey) url.searchParams.set("api_key", apiKey);
    this.socket = new WebSocket(url);
    this.socket.binaryType = "arraybuffer";
    this.socket.onopen = () => this.#requestViewport();
    this.socket.onmessage = (event) =>
      typeof event.data === "string" ? this.#onJson(JSON.parse(event.data)) : this.#onTile(event.data);
    this.socket.onclose = () => setTimeout(() => this.#connect(), 1000);
  }

  #send(message) {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(message));
  }

  /** 화면 상태 전송. requestAnimationFrame으로 모아 초당 최대 60번만 보낸다. */
  #requestViewport() {
    if (this.pending.viewport) return;
    this.pending.viewport = true;
    requestAnimationFrame(() => {
      this.pending.viewport = false;
      this.#send({
        type: "viewport", req_id: ++this.frameReqId, x_range: [this.viewStart, this.viewEnd],
        zoom: this.zoom, width_px: this.width, height_px: this.height, dpr: this.dpr,
      });
      this.#draw();
    });
  }

  #onJson(message) {
    switch (message.type) {
      case "frame_begin":
        if (message.req_id !== this.frameReqId) return;  // 이미 새 요청을 보냈다
        this.domain = { start: message.domain[0], end: message.domain[1] };
        this.frame = message;
        this.frameYKey.set(message.req_id, JSON.stringify(message.y_ranges));
        break;
      case "frame_end":
        this.frameYKey.delete(message.req_id - 64);
        this.onFrameEnd?.(message);
        break;
      case "crosshair":
        if (message.req_id !== this.queryReqId) return;  // 더 최근 호버가 있다
        this.crosshair = message;
        break;
      case "range_stats":
        if (this.selection) this.selection.stats = message;
        this.onRangeStats?.(message);
        break;
      case "data_appended": {
        // 화면과 겹치면 다시 요청한다. 바뀌지 않은 타일은 서버 캐시에서 바로 온다.
        const [low, high] = message.x_range ?? [-Infinity, Infinity];
        const ratio = (message.domain[1] - message.domain[0]) / (this.domain.end - this.domain.start);
        if (ratio !== 1) {
          // 격자가 2배로 넓어지면 같은 줌 번호가 2배 넓은 x를 덮는다. 화면 배율을 유지한다.
          this.zoom = Math.min(24, this.zoom + Math.round(Math.log2(ratio)));
          this.domain = { start: message.domain[0], end: message.domain[1] };
        }
        if (ratio !== 1 || (low < this.viewEnd && high >= this.viewStart)) this.#requestViewport();
        return;
      }
      case "error":
        console.warn("webchart:", message.code, message.message);
        return;
    }
    this.#draw();
  }

  async #onTile(buffer) {
    const view = new DataView(buffer);
    const reqId = view.getUint32(2);
    const [zoom, index, dpr] = [view.getUint8(6), view.getUint32(7), view.getUint8(11)];
    const yKey = this.frameYKey.get(reqId);
    if (yKey === undefined) return;
    // 디코딩은 메인 스레드 밖에서 (createImageBitmap)
    const bitmap = await createImageBitmap(new Blob([buffer.slice(HEADER_BYTES)], { type: "image/png" }));
    const key = `${yKey}|${zoom}|${index}|${dpr}`;
    this.bitmaps.delete(key);
    this.bitmaps.set(key, bitmap);
    if (this.bitmaps.size > BITMAP_CACHE_LIMIT) {
      const oldest = this.bitmaps.keys().next().value;
      this.bitmaps.get(oldest).close();
      this.bitmaps.delete(oldest);
    }
    this.#draw();
  }

  // --- 입력 ------------------------------------------------------------------

  #bindPointer() {
    const target = this.overlay;
    let drag = null;
    target.addEventListener("pointerdown", (event) => {
      target.setPointerCapture(event.pointerId);
      drag = { x: event.offsetX, start: this.viewStart, select: event.shiftKey };
      if (drag.select) this.selection = { start: this.pxToX(event.offsetX), end: this.pxToX(event.offsetX) };
    });
    target.addEventListener("pointermove", (event) => {
      if (drag?.select) {
        this.selection.end = this.pxToX(event.offsetX);
        this.#draw();
      } else if (drag) {
        this.viewStart = drag.start - (event.offsetX - drag.x) * this.xPerPx;
        this.#clampView();
        this.#requestViewport();
      }
      this.#hover(event.offsetX, event.offsetY);
    });
    target.addEventListener("pointerup", () => {
      if (drag?.select) {
        const [start, end] = [this.selection.start, this.selection.end].sort((a, b) => a - b);
        if (end > start) this.#send({ type: "select", req_id: ++this.queryReqId, x_range: [start, end] });
      }
      drag = null;
    });
    target.addEventListener("pointerleave", () => { this.crosshair = null; this.#draw(); });
    target.addEventListener("wheel", (event) => {
      event.preventDefault();
      this.zoomAt(event.offsetX, event.deltaY < 0 ? 1 : -1);
    }, { passive: false });
  }

  /** x 전체가 화면 폭 안에 들어오는 가장 깊은 줌으로 맞춘다 (가운데 정렬). */
  fitAll() {
    this.zoom = Math.max(0, Math.floor(Math.log2(this.width / this.options.tileSize)));
    const margin = (this.width * this.xPerPx - (this.domain.end - this.domain.start)) / 2;
    this.viewStart = this.domain.start - Math.max(0, margin);
    this.#requestViewport();
  }

  /** 화면 가운데가 데이터 밖으로 나가지 않게 이동 범위를 제한한다. */
  #clampView() {
    const half = (this.width * this.xPerPx) / 2;
    this.viewStart = Math.min(Math.max(this.viewStart, this.domain.start - half), this.domain.end - half);
  }

  /** 줌을 한 단계(2배) 바꾼다. 커서 아래의 x가 그 자리에 머문다. */
  zoomAt(px, step) {
    const zoom = Math.min(24, Math.max(0, this.zoom + step));
    if (zoom === this.zoom) return;
    const anchor = this.pxToX(px);
    this.zoom = zoom;
    this.viewStart = anchor - px * this.xPerPx;
    this.#clampView();
    this.crosshair = null;
    this.#requestViewport();
  }

  #hover(px, py) {
    const first = this.pending.hover === null;
    this.pending.hover = [px, py];
    if (!first) return;
    requestAnimationFrame(() => {
      const [x, y] = this.pending.hover;
      this.pending.hover = null;
      this.#send({ type: "hover", req_id: ++this.queryReqId, pixel_x: x, pixel_y: y });
    });
  }

  // --- 그리기 ----------------------------------------------------------------

  #draw() {
    this.#drawTiles();
    this.#drawOverlay();
  }

  #drawTiles() {
    const ctx = this.tiles.getContext("2d");
    const scale = this.tiles.width / this.width;
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    const frame = this.frame;
    if (!frame) return;
    const yKey = JSON.stringify(frame.y_ranges);
    for (const tile of frame.tiles) {
      // 같은 프레임 y범위로 그린 타일만 쓴다. 이 줌의 타일이 아직 없으면 그 프레임 줌의 타일을 늘려 그린다.
      const bitmap = this.bitmaps.get(`${yKey}|${tile.zoom}|${tile.index}|${frame.dpr}`);
      if (!bitmap) continue;
      const left = this.xToPx(tile.x_start);
      const right = this.xToPx(tile.x_end);
      if (right < 0 || left > this.width) continue;
      ctx.imageSmoothingEnabled = tile.zoom !== this.zoom;  // 1:1이면 보간하지 않는다
      ctx.drawImage(bitmap, left, 0, right - left, this.height);
    }
  }

  #drawOverlay() {
    const ctx = this.overlay.getContext("2d");
    const scale = this.overlay.width / this.width;
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    ctx.font = "11px system-ui, sans-serif";
    const frame = this.frame;
    if (frame) {
      // 서버가 계산한 눈금. 이동 중이면 프레임 이후 이동한 만큼 옮긴다.
      const shift = (frame.x_range[0] - this.viewStart) / this.xPerPx;
      const sameZoom = frame.zoom === this.zoom;
      ctx.fillStyle = "#555";
      ctx.textBaseline = "bottom";
      for (const tick of sameZoom ? frame.x_ticks : []) {
        const x = tick.pixel + shift;
        ctx.fillRect(x, this.height - 4, 1, 4);
        this.#label(ctx, tick.label, x + 3, this.height - 4);
      }
      ctx.textBaseline = "middle";
      for (const [axis, ticks] of Object.entries(frame.y_ticks)) {
        for (const tick of ticks) {
          if (tick.pixel < 8 || tick.pixel > this.height - 20) continue;  // 가장자리·x 라벨과 겹침
          const x = axis === "y" ? 0 : this.width - 4;
          ctx.fillRect(x, tick.pixel, 4, 1);
          const width = ctx.measureText(tick.label).width;
          this.#label(ctx, tick.label, axis === "y" ? 6 : this.width - width - 8, tick.pixel);
        }
      }
    }
    if (this.selection) {
      const [a, b] = [this.xToPx(this.selection.start), this.xToPx(this.selection.end)];
      ctx.fillStyle = "rgba(66,133,244,0.15)";
      ctx.fillRect(Math.min(a, b), 0, Math.abs(b - a), this.height);
      const stats = this.selection.stats;
      if (stats) {
        const lines = Object.entries(stats.series).map(([name, s]) =>
          `${name}: n=${s.count} min=${fmt(s.min)} max=${fmt(s.max)} mean=${fmt(s.mean)}`);
        this.#box(ctx, Math.min(a, b) + 4, 4, lines);
      }
    }
    if (this.crosshair && this.crosshair.points.length) {
      const px = this.xToPx(this.crosshair.points[0].x);
      ctx.fillStyle = "rgba(0,0,0,0.5)";
      ctx.fillRect(px, 0, 1, this.height);
      const lines = [this.crosshair.points[0].label,
        ...this.crosshair.points.map((p) => `${p.series}: ${fmt(p.y)}`)];
      this.#box(ctx, px + 8, 24, lines);
    }
  }

  #label(ctx, text, x, y) {
    const width = ctx.measureText(text).width;
    ctx.fillStyle = "rgba(255,255,255,0.75)";
    ctx.fillRect(x - 2, y - 7, width + 4, 14);
    ctx.fillStyle = "#333";
    ctx.fillText(text, x, y);
  }

  #box(ctx, x, y, lines) {
    const width = Math.max(...lines.map((line) => ctx.measureText(line).width)) + 12;
    const left = Math.min(x, this.width - width - 4);
    ctx.fillStyle = "rgba(255,255,255,0.92)";
    ctx.strokeStyle = "#999";
    ctx.fillRect(left, y, width, lines.length * 15 + 8);
    ctx.strokeRect(left, y, width, lines.length * 15 + 8);
    ctx.fillStyle = "#222";
    ctx.textBaseline = "top";
    lines.forEach((line, i) => ctx.fillText(line, left + 6, y + 5 + i * 15));
  }

  close() {
    this.socket.onclose = null;
    this.socket.close();
  }
}

function fmt(value) {
  return value === null || value === undefined ? "-" : Number(value).toPrecision(6);
}
