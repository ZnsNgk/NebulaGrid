import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

from app.core.config import get_settings

T = TypeVar("T")

_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_executor_workers: int | None = None


def get_file_operation_executor() -> ThreadPoolExecutor:
    """按配置创建专用文件操作线程池，避免磁盘 IO 占用 FastAPI 默认请求线程池。"""
    global _executor, _executor_workers
    workers = max(1, get_settings().file_operation_worker_threads)
    with _executor_lock:
        if _executor is None or _executor_workers != workers:
            if _executor is not None:
                # 测试或热重载可能调整配置；旧线程池只停止接收新任务，不中断已开始的文件写入。
                _executor.shutdown(wait=False, cancel_futures=False)
            _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ng-file-io")
            _executor_workers = workers
        return _executor


async def run_file_operation(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """在文件线程池中执行同步文件逻辑，让 async API 协程等待结果而不占用请求工作线程。"""
    loop = asyncio.get_running_loop()
    call = partial(func, *args, **kwargs)
    return await loop.run_in_executor(get_file_operation_executor(), call)


def submit_file_operation(func: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T]:
    """提交无需阻塞当前请求的后台文件任务，供打包和解压等长 IO 操作复用。"""
    call = partial(func, *args, **kwargs)
    return get_file_operation_executor().submit(call)


def shutdown_file_operation_executor() -> None:
    """服务关闭时停止接收新的文件任务，运行中的任务由下次启动的中断扫描兜底标记。"""
    global _executor, _executor_workers
    with _executor_lock:
        if _executor is None:
            return
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
        _executor_workers = None
