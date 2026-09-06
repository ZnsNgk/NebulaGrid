"""API 托管独立扫描进程：解析日志不与请求线程争用同一个 Python GIL。"""

import logging
import multiprocessing
import threading
import time

from sqlalchemy import text

from app.db.session import SessionLocal, engine
from app.services.task_progress_service import POLL_SECONDS, progress_tick
from app.services.task_timing import backfill_task_durations, fill_missing_finished_durations

logger = logging.getLogger(__name__)


def run_progress_worker(stop) -> None:
    # 显式 spawn 启动新解释器，不能 fork 已有 API 的数据库连接池和多线程状态。
    # 每个 API 可托管子进程，但 PostgreSQL 会话锁保证全站仅一个扫描器工作。
    # 锁连接不持事务、不锁任务行，子进程退出/连接断开后数据库自动释放锁。
    while not stop.is_set():
        try:
            with engine.connect() as connection:
                locked = connection.scalar(text("SELECT pg_try_advisory_lock(731290461)"))
                connection.commit()
                if locked:
                    try:
                        scan_loop(stop, connection)
                    finally:
                        connection.execute(text("SELECT pg_advisory_unlock(731290461)"))
                        connection.commit()
        except Exception:
            logger.exception("task progress worker failed; will retry")
        stop.wait(POLL_SECONDS)


def scan_loop(stop, lock_connection) -> None:
    backfilled = False
    last_duration_sweep = 0
    while not stop.is_set():
        # 锁连接若已断开应退出本轮，重新竞争单实例锁；每任务租约仍作为并发兜底。
        lock_connection.execute(text("SELECT 1"))
        lock_connection.commit()
        if not backfilled:
            try:
                backfilled = backfill_task_durations(SessionLocal, max_batches=1)
            except Exception:
                logger.exception("task duration backfill failed; will retry")
        if time.monotonic() - last_duration_sweep >= 60:
            try:
                fill_missing_finished_durations(SessionLocal)
                last_duration_sweep = time.monotonic()
            except Exception:
                logger.exception("legacy executor duration repair failed; will retry")
        try:
            progress_tick()
        except Exception:
            logger.exception("task progress tick failed")
        stop.wait(POLL_SECONDS)


def start_progress_worker(app) -> None:
    if getattr(app.state, "task_progress_thread", None) and app.state.task_progress_thread.is_alive():
        return
    context = multiprocessing.get_context("spawn")
    stop = context.Event()

    def supervise():
        # 托管线程只等待子进程，不读日志；意外退出后自动重启并从数据库游标接续。
        while not stop.is_set():
            process = context.Process(target=run_progress_worker, args=(stop,),
                                      name="nebulagrid-task-progress", daemon=True)
            process.start()
            try:
                while process.is_alive() and not stop.wait(POLL_SECONDS):
                    pass
            finally:
                process.join(timeout=1)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1)
                if not process.is_alive():
                    process.close()
            if not stop.is_set():
                logger.warning("task progress child exited; restarting")
                stop.wait(POLL_SECONDS)

    thread = threading.Thread(target=supervise, name="nebulagrid-progress-supervisor", daemon=True)
    app.state.task_progress_stop = stop
    app.state.task_progress_thread = thread
    thread.start()


def stop_progress_worker(app) -> None:
    if hasattr(app.state, "task_progress_stop"):
        app.state.task_progress_stop.set()
        # 文件系统阻塞只发生在子进程；退出时限制等待时间，不能拖住 API 重启。
        app.state.task_progress_thread.join(timeout=4)
