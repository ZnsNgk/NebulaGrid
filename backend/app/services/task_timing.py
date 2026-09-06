"""任务耗时的统一口径：只计算执行起止差，未知时间不能伪造为零。"""

from app.core.time_utils import ensure_local_datetime

ACTIVE_STATES = {"wait", "on_hold", "dispatching", "preparing", "starting", "running", "cancelling"}


def execution_duration(started_at, finished_at) -> float | None:
    start = ensure_local_datetime(started_at)
    finish = ensure_local_datetime(finished_at)
    if start is None or finish is None or finish < start:
        return None
    return (finish - start).total_seconds()


def save_missing_duration(db, task) -> None:
    """只更新仍然属于同次历史执行的缺值，避免回填与用户重新入队并发时写回旧时长。"""
    from sqlalchemy import update
    from app.db.models import Task

    duration = execution_duration(task.started_at, task.finished_at)
    if duration is not None:
        db.execute(update(Task).where(Task.id == task.id, Task.state == task.state,
                   Task.started_at == task.started_at, Task.finished_at == task.finished_at,
                   Task.duration_seconds.is_(None)).values(duration_seconds=duration))


def backfill_task_durations(session_factory, max_batches=None) -> bool:
    """升级后按主键分批补历史时长；持久游标可断点续跑，完全不读取历史日志。"""
    from sqlalchemy import select
    from app.db.models import Setting, Task

    key = "internal.task_duration_backfill_v1"
    batches = 0
    while max_batches is None or batches < max_batches:
        with session_factory() as db:
            marker = db.get(Setting, key)
            if marker is None:
                marker = Setting(key=key, value="0")
                db.add(marker)
                db.commit()
            marker = db.scalar(select(Setting).where(Setting.key == key).with_for_update())
            if marker.value == "done":
                return True
            rows = db.scalars(select(Task).where(Task.id > int(marker.value),
                                               ~Task.state.in_(ACTIVE_STATES), Task.duration_seconds.is_(None))
                              .order_by(Task.id).limit(500)).all()
            for task in rows:
                if task.state not in ACTIVE_STATES and task.duration_seconds is None:
                    save_missing_duration(db, task)
            marker.value = str(rows[-1].id) if rows else "done"
            db.commit()
            batches += 1
            if not rows:
                return True
    # API 托管扫描器每轮只补一批，避免首次升级连续写入全部历史记录。
    return False


def fill_missing_finished_durations(session_factory) -> None:
    """兼容升级期间仍在运行的旧 executor：它归档时不写新列，只补有效且缺值的记录。"""
    from sqlalchemy import select
    from app.db.models import Task

    with session_factory() as db:
        rows = db.scalars(select(Task).where(~Task.state.in_(ACTIVE_STATES),
                          Task.duration_seconds.is_(None), Task.started_at.is_not(None),
                          Task.finished_at >= Task.started_at).order_by(Task.id).limit(500)).all()
        for task in rows:
            save_missing_duration(db, task)
        db.commit()
