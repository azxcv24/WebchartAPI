"""렌더 동시성: I/O·집계용 스레드 풀과 그리기(paint)용 프로세스 풀.

측정(scripts/bench/bench_render_parallelism.py, 계획 문서 10장 Phase 4):
- fetch+project(pyarrow, numpy)는 GIL을 대부분 놓아 스레드로 충분하다.
- matplotlib 그리기는 GIL에 묶여 스레드 8개가 1개보다 빠르지 않다. 프로세스 8개는 약 6배.
그래서 그리기 단계만 프로세스 풀로 보낸다. 넘기는 것은 투영된 도형(작은 numpy 배열)뿐이다.
"""

import asyncio
import logging
import multiprocessing
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

from webchart.config import settings

logger = logging.getLogger("webchart.concurrency")

T = TypeVar("T")

_render_pool: ThreadPoolExecutor | None = None
_paint_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()


def init_render_pool() -> ThreadPoolExecutor:
    """스레드 풀과 (설정되어 있으면) 그리기 프로세스 풀을 만든다 (lifespan 시작 시)."""
    global _render_pool, _paint_pool
    with _pool_lock:
        if _render_pool is None:
            _render_pool = ThreadPoolExecutor(
                max_workers=settings.render_pool_size,
                thread_name_prefix="chart-render",
            )
        if _paint_pool is None and settings.paint_processes > 0:
            # spawn: 스레드가 있는 프로세스를 fork하면 잠금 상태가 복사되어 멈출 수 있다
            _paint_pool = ProcessPoolExecutor(
                max_workers=settings.paint_processes,
                mp_context=multiprocessing.get_context("spawn"),
            )
            # 워커는 첫 작업 때 하나씩 뜨고 matplotlib import에 약 1초가 걸린다.
            # 첫 화면이 그 비용을 치르지 않게 기동 시점에 미리 띄운다 (기다리지 않는다).
            for _ in range(settings.paint_processes):
                _paint_pool.submit(_warm_up)
            logger.info(f"그리기 프로세스 풀: {settings.paint_processes}개")
    return _render_pool


def get_render_pool() -> ThreadPoolExecutor:
    if _render_pool is not None:
        return _render_pool
    return init_render_pool()


async def run_in_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """막히는 함수를 렌더 스레드 풀에서 실행한다."""
    loop = asyncio.get_running_loop()
    pool = get_render_pool()
    return await loop.run_in_executor(pool, partial(func, *args, **kwargs))


def paint(func: Callable[..., T], *args: Any) -> T:
    """그리기 함수를 실행한다 (동기). 프로세스 풀이 있으면 거기서, 없으면 이 스레드에서.

    렌더 스레드 안에서 부른다. 프로세스 풀이 결과를 줄 때까지 이 스레드만 기다리므로
    이벤트 루프는 막히지 않는다. func와 인자는 pickle할 수 있어야 한다.
    """
    pool: Executor | None = _paint_pool
    if pool is None:
        return func(*args)
    return pool.submit(func, *args).result()


def _warm_up() -> None:
    """그리기 워커에서 matplotlib을 import하고 작은 그림을 한 번 그린다.

    첫 그림은 글꼴 캐시 등을 읽느라 수백 ms가 걸리므로 첫 요청 전에 치른다.
    """
    import time

    from webchart.domain.geometry import XRange
    from webchart.services.render.matplotlib_renderer import MatplotlibRenderer

    MatplotlibRenderer().render_raster([], XRange(0.0, 1.0), None, 16, 16)
    time.sleep(0.2)  # 같은 워커가 여러 warm-up을 연달아 받지 않게 잠시 붙잡는다


def shutdown_render_pool() -> None:
    global _render_pool, _paint_pool
    with _pool_lock:
        if _render_pool is not None:
            _render_pool.shutdown(wait=False)
            _render_pool = None
        if _paint_pool is not None:
            _paint_pool.shutdown(wait=False, cancel_futures=True)
            _paint_pool = None
