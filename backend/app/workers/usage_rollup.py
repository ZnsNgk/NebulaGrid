"""API 托管小时汇总进程：启动补缺、整点续算，原始历史查询不占用页面请求。"""

import logging
import multiprocessing
import threading
import time
from collections import deque

from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.time_utils import local_datetime
from app.db.models import Node
from app.db.session import SessionLocal, engine
from app.services.metrics_service import influx_enabled
from app.services.node_service import is_control_plane_node
from app.services.usage_rollup_service import (
    ensure_usage_bucket, hour_floor, missing_hours, read_hourly_rollups, rebuild_hour, retained_start,
)

logger = logging.getLogger(__name__)
LOCK_ID = 731290462


def run_usage_worker(stop):
    """独立 PostgreSQL 会话锁选举一个汇总器，API 多进程启动不会重复扫描历史。"""
    # spawn 子进程不继承 Uvicorn 的日志配置，显式启用进度日志方便升级时检查补算。
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    while not stop.is_set():
        try:
            with engine.connect() as connection:
                locked = connection.scalar(text(f"SELECT pg_try_advisory_lock({LOCK_ID})"))
                connection.commit()
                if locked:
                    try:
                        rollup_loop(stop, connection)
                    finally:
                        connection.execute(text(f"SELECT pg_advisory_unlock({LOCK_ID})"))
                        connection.commit()
        except Exception:
            logger.exception("小时用量汇总进程失败，30 秒后重试")
        stop.wait(30)


def rollup_loop(stop, lock_connection):
    settings = get_settings()
    ensure_usage_bucket(settings)
    queue = deque()
    retry_after = {}
    scanned_hour = None
    next_scan = 0
    while not stop.is_set():
        # 不持任务行锁；连接失效后立即退出并重新选举。写入使用固定时间戳作为重复执行兜底。
        lock_connection.execute(text("SELECT 1"))
        lock_connection.commit()
        now = local_datetime()
        boundary = hour_floor(now)
        if scanned_hour != boundary or (not queue and time.monotonic() >= next_scan):
            with SessionLocal() as db:
                node_ids = [node.id for node in db.scalars(select(Node).order_by(Node.id)) if not is_control_plane_node(node)]
            start = retained_start(now)
            existing = read_hourly_rollups(settings, node_ids, start, boundary)
            queue = deque(missing_hours(node_ids, start, boundary, existing))
            retry_after = {hour: deadline for hour, deadline in retry_after.items() if start <= hour < boundary}
            scanned_hour = boundary
            next_scan = time.monotonic() + 60
            logger.info("小时用量扫描：%s 个节点，%s 个小时待补算（最近小时优先）", len(node_ids), len(queue))
        job = None
        # 某小时失败不阻塞其他日期，失败项至少间隔一分钟重试，禁止紧密重试冲击监控库。
        for _ in range(len(queue)):
            candidate = queue.popleft()
            if retry_after.get(candidate[0], 0) <= time.monotonic():
                job = candidate
                break
            queue.append(candidate)
        if job is None:
            stop.wait(min(30, max(0.2, (boundary.timestamp() + 3600) - now.timestamp())))
            continue
        hour, missing_node_ids = job
        if hour < retained_start(now):
            continue
        try:
            rebuild_hour(settings, missing_node_ids, hour, now)
            retry_after.pop(hour, None)
            logger.info("小时用量已保存：%s，%s 个节点，待补 %s 小时", hour.isoformat(), len(missing_node_ids), len(queue))
        except Exception:
            logger.exception("小时用量补算失败：%s，保留缺口稍后重试", hour.isoformat())
            retry_after[hour] = time.monotonic() + 60
            queue.append(job)
        # 顺序、小批量重建；让出执行机会，且整点新增小时不必等整个 90 天补算结束。
        stop.wait(0.2)


def start_usage_worker(app):
    if not influx_enabled(get_settings()):
        return
    if getattr(app.state, "usage_rollup_thread", None) and app.state.usage_rollup_thread.is_alive():
        return
    context = multiprocessing.get_context("spawn")
    stop = context.Event()

    def supervise():
        # spawn 不继承 API 的数据库连接或线程；子进程异常退出后重新扫描已保存的小时接续。
        while not stop.is_set():
            process = context.Process(target=run_usage_worker, args=(stop,), name="nebulagrid-usage-rollup", daemon=True)
            process.start()
            try:
                while process.is_alive() and not stop.wait(1):
                    pass
            finally:
                process.join(timeout=1)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1)
                if not process.is_alive():
                    process.close()
            if not stop.is_set():
                logger.warning("小时用量子进程退出，稍后重新启动")
                stop.wait(5)

    thread = threading.Thread(target=supervise, name="nebulagrid-usage-supervisor", daemon=True)
    app.state.usage_rollup_stop = stop
    app.state.usage_rollup_thread = thread
    thread.start()


def stop_usage_worker(app):
    if hasattr(app.state, "usage_rollup_stop"):
        app.state.usage_rollup_stop.set()
        # 原始查询可能较慢，强制结束子进程可确保 API 重启不等待历史重建。
        app.state.usage_rollup_thread.join(timeout=4)
