"""用量口径集中在此：执行时间、GPU 卡时、去重后的服务器占用分别计算。"""

from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import validation_error
from app.core.rbac import Role, require_permission
from app.core.time_utils import ensure_local_datetime, local_datetime
from app.db.models import Node, Task, TaskAllocation, TaskRequirement, User, UserSupervisor
from app.services.auth_service import UserRecord
from app.services.metrics_service import get_daily_usage_metrics
from app.services.node_service import can_user_access_node, is_control_plane_node
from app.services.task_service import RUNNING_STATES, TERMINAL_STATES
from app.services.task_timing import execution_duration


def usage_users(user: UserRecord, db: Session):
    """包括零任务用户；导师关系每次读取数据库，解除关联立即收回统计权限。"""
    require_permission(user.role, "usage:read")
    statement = select(User.id, User.username, User.real_name, User.role)
    if user.role != Role.ADMIN:
        ids = select(UserSupervisor.student_id).where(UserSupervisor.supervisor_id == user.id)
        statement = statement.where(or_(User.id == user.id, User.id.in_(ids))) if user.role == Role.MENTOR else statement.where(User.id == user.id)
    return db.execute(statement.order_by(User.id)).all()


def empty_task_totals():
    return dict(submitted=0, executed=0, succeeded=0, failed=0, cancelled=0, running=0,
                waiting=0, other=0, runtime_seconds=0.0, gpu_hours=0.0,
                unknown_duration_tasks=0, unknown_gpu_tasks=0, states={})


def build_task_usage(user: UserRecord, db: Session):
    """仅扫描统计必要列；不加载命令、日志、进度解析器或任务列表的分页数据。"""
    users = usage_users(user, db)
    totals = {item.id: empty_task_totals() for item in users}
    now = local_datetime()
    latest_id = select(TaskAllocation.id).where(TaskAllocation.task_id == Task.id).order_by(
        TaskAllocation.allocated_at.desc(), TaskAllocation.id.desc()).limit(1).correlate(Task).scalar_subquery()
    statement = select(Task.user_id, Task.state, Task.started_at, Task.finished_at,
                       TaskRequirement.need_gpus, TaskAllocation.gpu_ids,
                       TaskAllocation.allocated_at, TaskAllocation.released_at).outerjoin(
        TaskRequirement, TaskRequirement.task_id == Task.id).outerjoin(
        TaskAllocation, TaskAllocation.id == latest_id).where(Task.user_id.in_(totals))
    # 流式读全量历史，避免首页原有的 200 条分页上限导致统计截断。
    for task in db.execute(statement.execution_options(yield_per=1000)):
        total = totals[task.user_id]
        total["submitted"] += 1
        total["states"][task.state] = total["states"].get(task.state, 0) + 1
        if task.state in {"succeeded", "cancelled"}:
            category = task.state
        elif task.state in TERMINAL_STATES:
            category = "other" if task.state == "unknown" else "failed"
        elif task.state in RUNNING_STATES:
            category = "running"
        else:
            category = "waiting" if task.state in {"wait", "on_hold"} else "other"
        total[category] += 1
        start = ensure_local_datetime(task.started_at)
        finish = now if task.state in RUNNING_STATES else ensure_local_datetime(task.finished_at)
        if start is None:
            # 排队、准备和执行前取消没有执行时长；异常历史的缺失时间单独提示。
            if task.state in TERMINAL_STATES and task.state not in {"cancelled", "dependency_failed"}:
                total["unknown_duration_tasks"] += 1
            continue
        total["executed"] += 1
        duration = execution_duration(start, min(finish, now) if finish else None)
        if duration is None:
            total["unknown_duration_tasks"] += 1
            continue
        total["runtime_seconds"] += duration
        allocated = ensure_local_datetime(task.allocated_at)
        released = ensure_local_datetime(task.released_at)
        if allocated is not None and allocated <= start and (released is None or released >= start):
            # 按实际分配的卡计数，CPU 任务自然为零；释放之后不能继续累加 GPU 时长。
            gpu_seconds = max(0.0, (min(finish, now, released or now) - start).total_seconds())
            total["gpu_hours"] += gpu_seconds * len(set(task.gpu_ids or [])) / 3600
        elif task.need_gpus != 0:
            total["unknown_gpu_tasks"] += 1
    all_totals = empty_task_totals()
    for total in totals.values():
        for key, value in total.items():
            if key != "states":
                all_totals[key] += value
        for key, value in total["states"].items():
            all_totals["states"][key] = all_totals["states"].get(key, 0) + value
    return {"generated_at": now.isoformat(), "personal": totals.get(user.id, empty_task_totals()),
            "summary": all_totals, "users": [dict(item._mapping, **totals[item.id]) for item in users]
            if user.role in {Role.MENTOR, Role.ADMIN} else []}


def merged_intervals(intervals):
    """同节点并发任务取区间并集；复用 GPU 或跨午夜不能重复计服务器时间。"""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def occupied_seconds(intervals, start, stop):
    """调用者先合并区间，再裁剪到自然日；今天只计算到本次请求时间。"""
    return sum(max(0.0, (min(end, stop) - max(begin, start)).total_seconds()) for begin, end in intervals)


def build_node_usage(user: UserRecord, db: Session, days: int = 7):
    users = usage_users(user, db)
    if days not in {7, 30, 90}:
        raise validation_error("usage days must be 7, 30 or 90")
    now = local_datetime()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    nodes = [node for node in db.scalars(select(Node).order_by(Node.id))
             if not is_control_plane_node(node) and (user.role == Role.ADMIN or can_user_access_node(user, node, db))]
    node_ids = [node.id for node in nodes]
    intervals = defaultdict(list)
    own_intervals = defaultdict(list)
    user_intervals = defaultdict(list)
    gpu_intervals = defaultdict(list)
    user_ids = {item.id for item in users}
    # 节点总体使用情况允许匿名聚合其他人的占用；身份明细只保留授权用户。
    statement = select(TaskAllocation.node_id, TaskAllocation.allocated_at,
                       TaskAllocation.released_at, TaskAllocation.gpu_ids, Task.user_id).join(Task, Task.id == TaskAllocation.task_id).where(
        TaskAllocation.node_id.in_(node_ids), TaskAllocation.allocated_at < now,
        or_(TaskAllocation.released_at.is_(None), TaskAllocation.released_at > start))
    for row in db.execute(statement.execution_options(yield_per=1000)) if node_ids else []:
        begin = ensure_local_datetime(row.allocated_at)
        end = ensure_local_datetime(row.released_at) or now
        if begin is None or end <= begin:
            continue
        interval = (max(begin, start), min(end, now))
        if interval[1] <= interval[0]:
            continue
        intervals[row.node_id].append(interval)
        if row.user_id == user.id:
            own_intervals[row.node_id].append(interval)
        if row.user_id in user_ids:
            user_intervals[(row.user_id, row.node_id)].append(interval)
            # 节点页统计资源占用卡时，包含分配到释放的准备/清理时间；不能套用任务页执行卡时。
            # 同用户、同节点、同卡的并发复用取并集，避免把同一张卡的同一小时重复统计。
            for gpu_id in set(row.gpu_ids or []):
                gpu_intervals[(row.user_id, row.node_id, gpu_id)].append(interval)
    user_totals = {item.id: 0.0 for item in users}
    user_gpu_hours = {item.id: 0.0 for item in users}
    node_gpu_hours = defaultdict(float)
    for (user_id, node_id, _), values in gpu_intervals.items():
        hours = occupied_seconds(merged_intervals(values), start, now) / 3600
        node_gpu_hours[(user_id, node_id)] += hours
        user_gpu_hours[user_id] += hours
    node_users = defaultdict(list)
    identities = {item.id: dict(item._mapping) for item in users}
    for (user_id, node_id), values in user_intervals.items():
        seconds = occupied_seconds(merged_intervals(values), start, now)
        user_totals[user_id] += seconds
        # 零时长行不出现在节点明细；学生接口也不返回身份列表，权限不能只靠前端隐藏。
        if seconds > 0 and user.role in {Role.MENTOR, Role.ADMIN}:
            node_users[node_id].append(dict(identities[user_id], occupied_seconds=seconds,
                                            gpu_hours=node_gpu_hours[(user_id, node_id)]))
    daily_metrics, metrics_status = get_daily_usage_metrics(node_ids, start, now)
    metrics_end = now.replace(minute=0, second=0, microsecond=0)
    result = []
    for node in nodes:
        merged = merged_intervals(intervals[node.id])
        own = merged_intervals(own_intervals[node.id])
        daily = []
        for index in range(days):
            begin = start + timedelta(days=index)
            end = min(begin + timedelta(days=1), now)
            seconds = occupied_seconds(merged, begin, end)
            metric = daily_metrics.get((node.id, begin.date().isoformat()), {})
            daily.append({"date": begin.date().isoformat(), "occupied_seconds": seconds,
                          "occupancy_percent": seconds / max(1, (end - begin).total_seconds()) * 100,
                          "own_occupied_seconds": occupied_seconds(own, begin, end),
                          "metric_hours": metric.get("hours", 0),
                          "expected_metric_hours": max(0, int((min(begin + timedelta(days=1), metrics_end) - begin).total_seconds() / 3600)),
                          "cpu_usage_percent": metric.get("cpu_usage"),
                          "gpu_usage_percent": metric.get("gpu_usage")})
        seconds = sum(day["occupied_seconds"] for day in daily)
        # 按实际已有的小时等权汇总，部分日期也参与；缺失小时不作零值，不占分母。
        averages = {}
        for field in ("cpu_usage_percent", "gpu_usage_percent"):
            values = [(day[field], day["metric_hours"]) for day in daily if day[field] is not None]
            hours = sum(count for _, count in values)
            averages[field] = sum(value * count for value, count in values) / hours if hours else None
        result.append({"id": node.id, "name": node.name, "daily": daily,
                       "users": sorted(node_users[node.id], key=lambda item: (-item["occupied_seconds"], item["id"])),
                       "occupied_seconds": seconds, "average_daily_seconds": seconds / days,
                       "occupancy_percent": seconds / max(1, (now - start).total_seconds()) * 100,
                       "own_occupied_seconds": sum(day["own_occupied_seconds"] for day in daily), **averages})
    # 提交数按创建日期，而占用按区间重叠；旧任务在本期运行仍要计算占用。
    submissions = dict(db.execute(select(Task.user_id, func.count()).where(
        Task.user_id.in_(user_ids), Task.created_at >= start, Task.created_at <= now).group_by(Task.user_id)).all())
    return {"generated_at": now.isoformat(), "start_at": start.isoformat(), "days": days,
            "metrics_end_at": metrics_end.isoformat(),
            "metrics_status": metrics_status, "nodes": result,
            "own_occupied_seconds": user_totals.get(user.id, 0),
            "users": [dict(item._mapping, submitted=submissions.get(item.id, 0),
                           occupied_seconds=user_totals[item.id], gpu_hours=user_gpu_hours[item.id]) for item in users]
            if user.role in {Role.MENTOR, Role.ADMIN} else []}
