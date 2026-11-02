"""렌더 병렬성 측정: matplotlib 페인트가 GIL에 묶이는지 (계획 문서 4.2, 9장 리스크).

타일 N장을 (1) 한 스레드로 차례로, (2) 스레드 풀로, (3) 프로세스 풀로 그려 처리량을 비교한다.
단계를 나눠 잰다: fetch+project(numpy/pyarrow, GIL을 대부분 놓는다)와 paint(matplotlib).

사용법:
    PYTHONPATH=src python scripts/bench/bench_render_parallelism.py --rows 5000000 --workers 8
"""

import argparse
import os
import shutil
import tempfile
import time
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

DATASET_ID = "parallel"


def prepare(rows: int) -> None:
    import numpy as np
    import pandas as pd

    from webchart.application.pyramids import build_pyramid
    from webchart.config import settings
    from webchart.services.ingest.upload import convert_to_parquet

    rng = np.random.default_rng(0)
    frame = pd.DataFrame(
        {
            "x": np.arange(rows, dtype=np.float64),
            "a": np.cumsum(rng.normal(0, 1, rows)),
            "b": np.cumsum(rng.normal(0, 1, rows)),
        }
    )
    source = settings.data_dir / "uploads" / "parallel.parquet"
    source.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(source, index=False)
    convert_to_parquet(source, DATASET_ID)
    build_pyramid(DATASET_ID, "x")


def _requests(zoom: int):
    from webchart.application.tiles import TileRequest, plan_frame
    from webchart.application.windows import lattice_for
    from webchart.config import settings
    from webchart.domain.geometry import TileAddress
    from webchart.models.chart import ChartSpec
    from webchart.services.data.access import get_dataset_meta

    meta = get_dataset_meta(DATASET_ID)
    spec = ChartSpec(dataset_id=DATASET_ID, x_axis={"column": "x"}, y_columns=["a", "b"])
    lattice = lattice_for(meta, "x", settings.tile_size)
    plan = plan_frame(meta, spec, lattice.domain, zoom, prefetch_radius=0)
    return [
        TileRequest(meta, spec, lattice, TileAddress(zoom, i), 1, plan.y_ranges)
        for i in range(lattice.tile_count(zoom))
    ]


def fetch_and_project(index_and_zoom: tuple[int, int]):
    from webchart.application.tiles import tile_drawings

    index, zoom = index_and_zoom
    return tile_drawings(_requests(zoom)[index])


def full_tile(index_and_zoom: tuple[int, int]) -> int:
    from webchart.application.tiles import PipelineTileRenderer

    index, zoom = index_and_zoom
    return len(PipelineTileRenderer._render_sync(_requests(zoom)[index]).image)


def timed(executor: Executor | None, work, items) -> float:
    started = time.perf_counter()
    if executor is None:
        for item in items:
            work(item)
    else:
        list(executor.map(work, items))
    return time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=5_000_000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--zoom", type=int, default=5)
    args = parser.parse_args()

    work_dir = Path(tempfile.mkdtemp(prefix="webchart-parallel-"))
    os.environ["WEBCHART_DATA_DIR"] = str(work_dir)
    try:
        prepare(args.rows)
        items = [(i, args.zoom) for i in range(2**args.zoom)]
        full_tile(items[0])  # 워밍업 (import, mmap)

        print(f"\n### 렌더 병렬성: {args.rows:,}행, 줌 {args.zoom} 타일 {len(items)}장\n")
        print("| 방식 | fetch+project (s) | 전체 타일 (s) | 타일/초 |")
        print("|---|---:|---:|---:|")
        for label, make in (
            ("1 스레드", lambda: None),
            (f"스레드 {args.workers}", lambda: ThreadPoolExecutor(args.workers)),
            (f"프로세스 {args.workers}", lambda: ProcessPoolExecutor(args.workers)),
        ):
            executor = make()
            if isinstance(executor, ProcessPoolExecutor):
                list(executor.map(full_tile, items[: args.workers]))  # 워커 워밍업
            fetch_s = timed(executor, fetch_and_project, items)
            full_s = timed(executor, full_tile, items)
            if executor is not None:
                executor.shutdown()
            print(f"| {label} | {fetch_s:.2f} | {full_s:.2f} | {len(items) / full_s:.1f} |")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
