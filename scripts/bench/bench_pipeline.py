"""파이프라인 단계별 벤치마크: 생성 → 인제스트 → 피라미드 → 타일 → 정적 렌더.

개선 전후를 같은 조건으로 비교하기 위한 기준선 측정 도구다. 결과는 마크다운 표로
출력하며, 기록할 만한 결과는 docs/improvement-plan.md에 옮겨 적는다.

사용법:
    PYTHONPATH=src python scripts/bench/bench_pipeline.py --rows 5000000 --series 2

측정 조건:
- 타일 캐시를 거치지 않는다(PipelineTileRenderer 직접 호출). 항상 처음 그리는 경우(cold)를 잰다.
- 같은 프로세스에서 단계를 차례로 실행한다. 메모리는 프로세스 최대 RSS(누적)이다.
- 실패한 단계(예: 행 수 상한 초과)도 결과로 기록한다. 그 자체가 개선 대상이다.
"""

import argparse
import asyncio
import os
import resource
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

DATASET_ID = "bench"


@dataclass
class StepResult:
    name: str
    seconds: float
    peak_rss_mb: float
    note: str


def peak_rss_mb() -> float:
    # 리눅스에서 ru_maxrss 단위는 KB다
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def measure(name: str, action: Callable[[], Any], describe: Callable[[Any], str]) -> StepResult:
    """action을 실행해 시간과 메모리를 잰다. 예외도 결과로 남긴다."""
    started = time.perf_counter()
    try:
        outcome = action()
        note = describe(outcome)
    except Exception as error:  # 벤치마크는 실패도 기록 대상이다
        note = f"실패: {type(error).__name__}: {str(error)[:80]}"
    return StepResult(name, time.perf_counter() - started, peak_rss_mb(), note)


def write_source(path: Path, rows: int, series: int, chunk_rows: int) -> int:
    """합성 데이터를 청크 단위로 Parquet에 쓴다 (메모리 사용을 청크 크기로 묶는다)."""
    from webchart.testing.synthetic import SyntheticSpec, iter_chunks

    spec = SyntheticSpec(rows=rows, series=series, spikes_per_series=100, seed=0)
    writer: pq.ParquetWriter | None = None
    try:
        for chunk in iter_chunks(spec, chunk_rows):
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(path, table.schema)
            writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()
    return path.stat().st_size


def run(rows: int, series: int, chunk_rows: int) -> list[StepResult]:
    # settings는 import 시점에 환경변수를 읽으므로 경로 설정 후에 import한다
    from webchart.application.pyramids import build_pyramid
    from webchart.application.static_chart import render_static_chart
    from webchart.application.tiles import PipelineTileRenderer, TileRequest, plan_frame
    from webchart.application.windows import lattice_for
    from webchart.config import settings
    from webchart.domain.geometry import TileAddress
    from webchart.models.chart import AxisConfig, ChartSpec
    from webchart.services.data.access import get_dataset_meta
    from webchart.services.ingest.upload import convert_to_parquet
    from webchart.testing.synthetic import X_COLUMN, series_column

    y_columns = [series_column(i) for i in range(series)]
    spec = ChartSpec(dataset_id=DATASET_ID, x_axis=AxisConfig(column=X_COLUMN), y_columns=y_columns)
    source = settings.data_dir / "uploads" / "bench.parquet"
    source.parent.mkdir(parents=True, exist_ok=True)

    def render_tile(zoom: int) -> tuple[bytes, str]:
        """가운데 타일 하나를 캐시 없이 그린다 (cold)."""
        meta = get_dataset_meta(DATASET_ID)
        lattice = lattice_for(meta, X_COLUMN, settings.tile_size)
        tile = TileAddress(zoom, lattice.tile_count(zoom) // 2)
        plan = plan_frame(meta, spec, lattice.tile_range(tile), zoom, prefetch_radius=0)
        request = TileRequest(meta, spec, plan.lattice, tile, 1, plan.y_ranges)
        rendered = asyncio.run(PipelineTileRenderer().render(request))
        return rendered.image, rendered.source

    def render_static() -> tuple[bytes, str]:
        rendered = render_static_chart(spec)
        exact = "exact" if rendered.is_exact else "근사"
        return rendered.image, f"{rendered.source}, {exact}, {rendered.point_count:,}점"

    results = [
        measure(
            "합성 데이터 쓰기",
            lambda: write_source(source, rows, series, chunk_rows),
            lambda size: f"{size / 1e6:.0f} MB",
        ),
        measure(
            "인제스트 (정렬 검사 + 로우그룹 Parquet)",
            lambda: convert_to_parquet(source, DATASET_ID),
            lambda meta: f"{meta.partition_count} 파일, 정렬={meta.sort_column}",
        ),
        measure("타일 z=0 (피라미드 없음, 원본 집계)", lambda: render_tile(0), _image_note),
        measure("정적 렌더 전체 범위 (피라미드 없음)", render_static, _image_note),
        measure(
            "피라미드 빌드",
            lambda: build_pyramid(DATASET_ID, X_COLUMN, columns=y_columns),
            lambda info: f"L0..L{info.max_level}, {info.size_bytes / 1e6:.0f} MB",
        ),
        measure("타일 z=0 (피라미드)", lambda: render_tile(0), _image_note),
        measure("정적 렌더 전체 범위 (피라미드 가능)", render_static, _image_note),
    ]
    deep_zoom = _deep_zoom()
    deep_label = f"타일 z={deep_zoom} (원본, 좁은 범위)"
    results.append(measure(deep_label, lambda: render_tile(deep_zoom), _image_note))
    return results


def _deep_zoom() -> int:
    """피라미드 최대 레벨을 넘어 원본을 읽게 되는 가장 얕은 줌 (레벨 = 줌 + 9)."""
    from webchart.services.data.access import get_dataset_meta
    from webchart.testing.synthetic import X_COLUMN

    info = get_dataset_meta(DATASET_ID).pyramids.get(X_COLUMN)
    return 12 if info is None else info.max_level - 9 + 1


def _image_note(outcome: tuple[bytes, str]) -> str:
    image, source = outcome
    return f"{len(image) / 1024:.0f} KB, {source}"


def print_table(rows: int, series: int, results: list[StepResult]) -> None:
    print(f"\n### 벤치마크: {rows:,}행 × {series}시리즈\n")
    print("| 단계 | 시간(s) | 최대 RSS(MB) | 비고 |")
    print("|---|---:|---:|---|")
    for result in results:
        print(
            f"| {result.name} | {result.seconds:.2f} | {result.peak_rss_mb:,.0f} | {result.note} |"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--series", type=int, default=2)
    parser.add_argument("--chunk-rows", type=int, default=1_000_000)
    parser.add_argument("--work-dir", type=Path, default=None, help="기본값: 임시 디렉터리")
    parser.add_argument("--keep", action="store_true", help="끝난 뒤 작업 디렉터리를 남긴다")
    args = parser.parse_args()

    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="webchart-bench-"))
    os.environ["WEBCHART_DATA_DIR"] = str(work_dir)
    try:
        results = run(args.rows, args.series, args.chunk_rows)
        print_table(args.rows, args.series, results)
    finally:
        if args.keep:
            print(f"\n작업 디렉터리: {work_dir}", file=sys.stderr)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
