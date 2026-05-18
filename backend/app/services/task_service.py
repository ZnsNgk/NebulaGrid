from itertools import count

from app.core.errors import forbidden, not_found, validation_error
from app.core.path_resolver import resolve_user_visible_path
from app.core.rbac import Role, require_permission
from app.schemas.tasks import TaskCreateRequest, TaskGuardInfo, TaskInfo, TaskUpdateRequest
from app.services.audit_service import record_audit, utc_now
from app.services.auth_service import UserRecord

_TASK_ID = count(1)
_TASKS: list[TaskInfo] = []


def list_tasks(
    user: UserRecord,
    state: str | None,
    search: str | None,
    page: int,
    page_size: int,
) -> tuple[list[TaskInfo], int]:
    """按角色过滤任务列表，并支持状态和关键词筛选。"""
    require_permission(user.role, "tasks:read")
    items = [task for task in _TASKS if can_view_task(user, task)]
    if state:
        items = [task for task in items if task.state == state]
    if search:
        lowered = search.lower()
        items = [
            task
            for task in items
            if lowered in task.task_id.lower() or lowered in task.description.lower()
        ]
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], len(items)


def create_task(user: UserRecord, payload: TaskCreateRequest) -> TaskInfo:
    """提交任务并进入 wait 或 on_hold 状态，真实资源分配交给调度器。"""
    require_permission(user.role, "tasks:create")
    if user.state != "enabled":
        raise forbidden("disabled user cannot submit tasks")
    resolve_user_visible_path(payload.workdir, user.id)
    numeric_id = next(_TASK_ID)
    task = TaskInfo(
        id=numeric_id,
        task_id=f"NG-{numeric_id:06d}",
        user_id=user.id,
        description=payload.description,
        env_id=payload.env_id,
        workdir=payload.workdir,
        command=payload.command,
        state="on_hold" if payload.on_hold else "wait",
        priority=payload.priority,
        on_hold=payload.on_hold,
        created_at=utc_now(),
        requirement=payload.requirement,
    )
    _TASKS.append(task)
    record_audit(user.id, "task.create", "task", task.task_id)
    return task


def get_task_for_user(user: UserRecord, task_id: str) -> TaskInfo:
    """获取用户可见任务详情，不可见时按 NOT_FOUND 处理以减少越权探测。"""
    task = find_task(task_id)
    if task is None or not can_view_task(user, task):
        raise not_found("task not found")
    return task


def update_task(user: UserRecord, task_id: str, payload: TaskUpdateRequest) -> TaskInfo:
    """编辑等待或挂起任务，运行中任务不可修改以保护调度一致性。"""
    task = get_task_for_user(user, task_id)
    require_task_owner_or_admin(user, task)
    if task.state not in {"wait", "on_hold"}:
        raise validation_error("only wait or on_hold tasks can be edited")
    data = payload.model_dump(exclude_unset=True)
    if "workdir" in data and data["workdir"] is not None:
        resolve_user_visible_path(data["workdir"], task.user_id)
    for key, value in data.items():
        if key == "requirement" and payload.requirement is not None:
            task.requirement = payload.requirement
            continue
        setattr(task, key, value)
    if "on_hold" in data:
        task.state = "on_hold" if task.on_hold else "wait"
    record_audit(user.id, "task.update", "task", task.task_id, detail_json=data)
    return task


def cancel_task(user: UserRecord, task_id: str) -> TaskInfo:
    """取消任务并记录完成时间，后续执行器会负责远程进程终止。"""
    task = get_task_for_user(user, task_id)
    require_task_owner_or_admin(user, task)
    if task.state in {"succeeded", "failed", "cancelled"}:
        raise validation_error("finished task cannot be cancelled")
    task.state = "cancelled"
    task.finished_at = utc_now()
    record_audit(user.id, "task.cancel", "task", task.task_id)
    return task


def resubmit_task(user: UserRecord, task_id: str) -> TaskInfo:
    """基于历史任务创建一条新任务，保留命令和资源需求用于快速重跑。"""
    source = get_task_for_user(user, task_id)
    require_task_owner_or_admin(user, source)
    payload = TaskCreateRequest(
        description=source.description,
        env_id=source.env_id,
        workdir=source.workdir,
        command=source.command,
        priority=source.priority,
        on_hold=False,
        requirement=source.requirement,
    )
    task = create_task(user, payload)
    record_audit(user.id, "task.resubmit", "task", task.task_id, detail_json={"source": task_id})
    return task


def get_task_log(user: UserRecord, task_id: str, tail: str | None) -> str:
    """返回任务日志占位内容，真实版本会读取 /data/logs/task_logs。"""
    task = get_task_for_user(user, task_id)
    return f"[{task.task_id}] log tail={tail or 'default'}\nlog storage is not connected yet\n"


def get_task_guard(user: UserRecord, task_id: str) -> TaskGuardInfo:
    """返回运行时守护占位摘要，普通用户只看到自己任务的脱敏信息。"""
    task = get_task_for_user(user, task_id)
    return TaskGuardInfo(
        task_id=task.task_id,
        root_pid=None,
        process_group_id=None,
        allocated_gpu_ids=[],
        observed_gpu_uuids=[],
        violation_count=0,
        state="not_started",
        last_check_at=None,
    )


def find_task(task_id: str) -> TaskInfo | None:
    """按业务 task_id 查找任务，避免路由层直接访问内存列表。"""
    return next((task for task in _TASKS if task.task_id == task_id), None)


def can_view_task(user: UserRecord, task: TaskInfo) -> bool:
    """判断任务是否对用户可见，管理员和导师先拥有全量可见性。"""
    return user.role in {Role.ADMIN, Role.MENTOR, Role.VIEWER} or task.user_id == user.id


def require_task_owner_or_admin(user: UserRecord, task: TaskInfo) -> None:
    """断言用户是任务所有者或管理员，失败时返回 403。"""
    if user.role != Role.ADMIN and task.user_id != user.id:
        raise forbidden("task owner or admin required")
