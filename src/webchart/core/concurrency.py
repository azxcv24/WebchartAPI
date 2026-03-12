import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

from webchart.config import settings

T = TypeVar("T")

_render_pool: ThreadPoolExecutor | None = None


def get_render_pool() -> ThreadPoolExecutor:
    global _render_pool
    if _render_pool is None:
        _render_pool = ThreadPoolExecutor(
            max_workers=settings.render_pool_size,
            thread_name_prefix="chart-render",
        )
    return _render_pool


async def run_in_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking function in the render thread pool."""
    loop = asyncio.get_running_loop()
    pool = get_render_pool()
    return await loop.run_in_executor(pool, partial(func, *args, **kwargs))


def shutdown_render_pool() -> None:
    global _render_pool
    if _render_pool is not None:
        _render_pool.shutdown(wait=False)
        _render_pool = None
