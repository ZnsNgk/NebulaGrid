"""执行区日志增量读取及摘要查询；文件 I/O 不占用任务状态行锁。"""

import copy
import hashlib
import os
import stat
import time
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.models import Setting, Task, TaskAllocation, TaskProgress
from app.db.session import SessionLocal
from app.services.log_progress import LogProgressParser, PARSER_VERSION

INTERVAL_KEY = "task.progress.interval_seconds"
READ_CHUNK_KEY = "task.progress.read_chunk_mb"
READ_LIMIT = 4 * 1024 * 1024
POLL_SECONDS = 5
MAX_TASKS_PER_TICK = 8


def scan_interval(db) -> int:
    value = db.scalar(select(Setting.value).where(Setting.key == INTERVAL_KEY))
    try:
        return max(10, min(3600, int(value or "60")))
    except (ValueError, TypeError):
        return 60


def run_key(task, allocation_id) -> str:
    # 同一任务重新执行或编辑路径后不得沿用旧进度；不依赖新版 runner 协议，兼容升级时在跑任务。
    text = f"{PARSER_VERSION}:{allocation_id}:{task.started_at}:{task.log_path}"
    return hashlib.sha256(text.encode()).hexdigest()


def read_log(path: str, previous: dict, now: float, interval: int, read_limit: int | None = None) -> tuple[dict, dict]:
    """从零补读旧运行任务，追上后只读新增；限量读取和游标持久化保障公平与重启恢复。"""
    root = Path(get_settings().task_log_root).resolve()
    candidate = Path(path).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("任务日志路径超出配置目录")
    state = copy.deepcopy(previous)
    with candidate.open("rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("任务日志必须是普通文件")
        identity = f"{info.st_dev}:{info.st_ino}"
        offset = state.get("offset", 0)
        rotated = state.get("identity") != identity or offset > info.st_size
        if not rotated and offset and state.get("anchor"):
            file.seek(max(0, offset - 64))
            rotated = hashlib.sha256(file.read(min(offset, 64))).hexdigest() != state["anchor"]
        if rotated:
            state = {}
            offset = 0
        parser = LogProgressParser(state.get("parser", {}))
        file.seek(offset)
        data = file.read(READ_LIMIT if read_limit is None else read_limit)
        parser.feed(data)
        offset += len(data)
        # 采样时固定本次文件大小；读取期间仍有输出时下一轮继续处理，不把扫描时间分配给旧行。
        catching_up = offset < info.st_size
        if not catching_up:
            parser.observe(now)
        summary = parser.summary(now, interval)
        if catching_up:
            summary.update(text="日志读取中", remaining_seconds=None, scope="unknown",
                           reason="正在补读本次执行的配置和进度", updated_at=None, stale=False,
                           catchup={"read_bytes": offset, "total_bytes": info.st_size,
                                    "percent": round(100 * offset / info.st_size, 1)})
        file.seek(max(0, offset - 64))
        anchor = hashlib.sha256(file.read(min(offset, 64))).hexdigest()
    state.update(offset=offset, identity=identity, anchor=anchor, parser=parser.state, catching_up=catching_up)
    return state, summary


def scan_task(task_id: int, interval: int, session_factory=SessionLocal, read_limit: int | None = None) -> None:
    now = time.time()
    with session_factory() as db:
        task = db.get(Task, task_id)
        if task is None or task.state != "running" or not task.log_path:
            return
        allocation_id = db.scalar(select(func.max(TaskAllocation.id)).where(TaskAllocation.task_id == task_id))
        key = run_key(task, allocation_id)
        row = db.get(TaskProgress, task_id)
        if row is None:
            db.add(TaskProgress(task_id=task_id))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            row = db.get(TaskProgress, task_id)
        # 运行态必须再次核对；版本条件让旧扫描即便租约超时也不能覆盖新扫描结果。
        if row.lease_until > now or (row.run_key == key and now - row.scanned_at < interval):
            return
        version = row.version
        old_summary = row.summary
        previous = copy.deepcopy(row.parser_state) if row.run_key == key else {}
        path = task.log_path
        claimed = db.execute(update(TaskProgress).where(TaskProgress.task_id == task_id,
                              TaskProgress.version == version, TaskProgress.lease_until <= now)
                             .values(lease_until=now + 120, version=version + 1))
        db.commit()
        if not claimed.rowcount:
            return
    try:
        state, summary = read_log(path, previous, now, interval, read_limit)
    except (OSError, ValueError):
        # 文件尚未创建、NFS 暂不可读或路径无效时保留游标，下轮重试，不影响任务执行。
        state = previous
        summary = {"text": "日志暂不可读", "remaining_seconds": None, "scope": "unknown",
                   "reason": "等待日志文件恢复", "updated_at": None, "stale": True}
    with session_factory() as db:
        task = db.get(Task, task_id)
        latest_id = db.scalar(select(func.max(TaskAllocation.id)).where(TaskAllocation.task_id == task_id))
        if task is None or task.state != "running" or run_key(task, latest_id) != key:
            # 扫描中任务结束/重跑，丢弃结果并释放自己的租约。
            db.execute(update(TaskProgress).where(TaskProgress.task_id == task_id, TaskProgress.version == version + 1)
                       .values(lease_until=0))
        else:
            # 补读也限制速率，避免升级时大量旧日志持续抢占共享盘和数据库。
            scanned_at = now - interval + POLL_SECONDS if state.get("catching_up") else now
            # 租约领取和内部游标推进不是页面变化；补读期间不要反复触发整页刷新。
            display_keys = ("text", "remaining_seconds", "scope", "reason", "stale", "estimate_kind")
            changed = any((old_summary or {}).get(k) != summary.get(k) for k in display_keys)
            # 进度条按约 5% 的变化通知页面，避免每个读取块都引发列表刷新；手动刷新取最新值。
            old_bucket = int(((old_summary or {}).get("catchup") or {}).get("percent", -5) // 5)
            new_bucket = int((summary.get("catchup") or {}).get("percent", -5) // 5)
            changed = changed or old_bucket != new_bucket
            db.execute(update(TaskProgress).where(TaskProgress.task_id == task_id, TaskProgress.version == version + 1)
                       .values(run_key=key, parser_state=state, summary=summary, scanned_at=scanned_at,
                               lease_until=0, version=version + 2,
                               summary_version=TaskProgress.summary_version + int(changed)))
        db.commit()


def progress_tick(session_factory=SessionLocal) -> None:
    """只枚举执行区中确实已运行的任务，准备/停止中的任务不读日志。"""
    with session_factory() as db:
        # 两项配置一次查询，热更新读取块大小不会增加空轮询 SQL，也不重建已有解析游标。
        options = dict(db.execute(select(Setting.key, Setting.value).where(
            Setting.key.in_([INTERVAL_KEY, READ_CHUNK_KEY]))).all())
        try:
            interval = max(10, min(3600, int(options.get(INTERVAL_KEY, "60"))))
        except (ValueError, TypeError):
            interval = 60
        try:
            chunk_mb = max(1, min(16, int(options.get(READ_CHUNK_KEY, "4"))))
        except (ValueError, TypeError):
            chunk_mb = 4
        # 空轮询只取轻量元数据，不能逐任务加载完整 ORM / JSON 后才判断是否到期。
        latest_allocation = select(func.max(TaskAllocation.id)).where(
            TaskAllocation.task_id == Task.id).correlate(Task).scalar_subquery()
        rows = db.execute(select(Task.id, Task.started_at, Task.log_path,
                                 latest_allocation.label("allocation_id"), TaskProgress.run_key,
                                 TaskProgress.scanned_at, TaskProgress.lease_until)
                          .outerjoin(TaskProgress, TaskProgress.task_id == Task.id)
                          .where(Task.state == "running", Task.log_path.is_not(None))
                          .order_by(func.coalesce(TaskProgress.scanned_at, 0), Task.id)).all()
    now = time.time()
    due = [row.id for row in rows if (row.lease_until or 0) <= now and
           (row.run_key != run_key(row, row.allocation_id) or now - (row.scanned_at or 0) >= interval)]
    # 按上次扫描时间排序并设置全轮上限，大日志不能把后面的任务永久饿死。
    for task_id in due[:MAX_TASKS_PER_TICK]:
        try:
            scan_task(task_id, interval, session_factory, read_limit=chunk_mb * 1024 * 1024)
        except Exception:
            # 单个损坏日志/数据库竞争不能阻止后面的任务获得扫描机会。
            import logging
            logging.getLogger(__name__).exception("task progress scan failed: %s", task_id)


def load_progress(tasks, allocations, db) -> dict:
    """列表批量读取摘要，原始日志和解析内部状态不进入 API 响应。"""
    active = {task.id: task for task in tasks if task.state == "running"}
    if not active:
        return {}
    rows = db.execute(select(TaskProgress.task_id, TaskProgress.run_key, TaskProgress.summary)
                      .where(TaskProgress.task_id.in_(active))).all()
    interval = scan_interval(db)
    result = {}
    for row in rows:
        task = active[row.task_id]
        allocation = allocations.get(task.id)
        if row.run_key != run_key(task, allocation.id if allocation else None):
            continue
        summary = dict(row.summary or {})
        updated = summary.get("updated_at")
        if updated and time.time() - updated > max(180, 3 * interval):
            summary.update(remaining_seconds=None, stale=True, reason="进度长时间未更新")
        result[task.id] = summary
    return result
