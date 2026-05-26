from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import forbidden, not_found, validation_error
from app.core.path_resolver import resolve_user_visible_path
from app.core.rbac import Role, require_permission
from app.core.time_utils import ensure_local_datetime, local_datetime
from app.db.models import (
    Env,
    Gpu,
    Node,
    Task,
    TaskAllocation,
    TaskDependency,
    TaskEvent,
    TaskRequirement as TaskRequirementModel,
    TaskRuntimeGuard,
    User,
    UserSupervisor,
)
from app.schemas.tasks import (
    TaskBatchCreateRequest,
    TaskBatchCreateResult,
    TaskCreateRequest,
    TaskDeletePreview,
    TaskDeleteResult,
    TaskGuardInfo,
    TaskInfo,
    TaskRequirement,
    TaskUpdateRequest,
)
from app.services.audit_service import record_audit
from app.services.auth_service import UserRecord
from app.services.env_service import get_env_for_user
from app.services.node_service import can_user_access_node

WAIT_STATES = {"wait", "on_hold"}
RUNNING_STATES = {"dispatching", "starting", "running", "preparing"}
TERMINAL_STATES = {
    "succeeded",
    "failed",
    "cancelled",
    "alloc_error",
    "offline_error",
    "offline",
    "node_lost",
    "dependency_failed",
}


def list_tasks(
    user: UserRecord,
    db: Session,
    state: str | None,
    search: str | None,
    page: int,
    page_size: int,
    all_history: bool = False,
) -> tuple[list[TaskInfo], int]:
    """按角色可见性分页返回任务，历史区默认分页，显式加载时返回全部可见历史。"""
    require_permission(user.role, "tasks:read")
    statement = (
        select(Task)
        .options(selectinload(Task.requirement))
        .where(task_visibility_condition(user, db))
    )
    zone = (state or "").lower()
    state_filter = task_state_condition(zone)
    if state_filter is not None:
        statement = statement.where(state_filter)
    if search:
        keyword = f"%{search.strip()}%"
        statement = statement.where(
            or_(Task.task_id.ilike(keyword), Task.description.ilike(keyword), Task.command.ilike(keyword))
        )
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    ordered = order_task_query(statement, zone)
    if not (all_history and zone in {"history", "hist", "finished"}):
        ordered = ordered.offset((page - 1) * page_size).limit(page_size)
    tasks = db.scalars(ordered).all()
    return build_task_infos(tasks, db), total


def task_change_cursor(user: UserRecord, db: Session) -> dict[str, int | dict[str, dict[str, int]]]:
    """返回当前用户可见任务的轻量版本号，供 SSE 判断是否需要刷新任务列表。"""
    require_permission(user.role, "tasks:read")
    visible_tasks = select(Task.id, Task.state).where(task_visibility_condition(user, db)).subquery()
    state_rows = db.execute(
        select(
            visible_tasks.c.state,
            func.count(visible_tasks.c.id),
            func.max(visible_tasks.c.id),
        )
        .select_from(visible_tasks)
        .group_by(visible_tasks.c.state)
    ).all()
    event_rows = db.execute(
        select(visible_tasks.c.state, func.max(TaskEvent.id))
        .select_from(visible_tasks)
        .join(TaskEvent, TaskEvent.task_id == visible_tasks.c.id)
        .group_by(visible_tasks.c.state)
    ).all()
    total = sum(int(row[1] or 0) for row in state_rows)
    max_task_id = max((int(row[2] or 0) for row in state_rows), default=0)
    max_event_id = max((int(row[1] or 0) for row in event_rows), default=0)
    return {
        "total": int(total),
        "max_task_id": int(max_task_id),
        "max_event_id": int(max_event_id),
        "zones": build_task_zone_cursors(state_rows, event_rows),
    }


def build_task_zone_cursors(state_rows, event_rows) -> dict[str, dict[str, int]]:
    """把数据库按状态聚合的版本号折叠成前端三个任务分区。"""
    cursors = {
        "wait": {"count": 0, "max_task_id": 0, "max_event_id": 0},
        "running": {"count": 0, "max_task_id": 0, "max_event_id": 0},
        "history": {"count": 0, "max_task_id": 0, "max_event_id": 0},
    }
    event_by_state = {row[0]: int(row[1] or 0) for row in event_rows}
    for state, count, max_task_id in state_rows:
        zone = task_zone_name(state)
        if zone is None:
            continue
        cursor = cursors[zone]
        cursor["count"] += int(count or 0)
        cursor["max_task_id"] = max(cursor["max_task_id"], int(max_task_id or 0))
        cursor["max_event_id"] = max(cursor["max_event_id"], event_by_state.get(state, 0))
    return cursors


def task_zone_name(state: str | None) -> str | None:
    """把数据库任务状态映射到任务管理页分区，未知状态不触发分区刷新。"""
    if state in WAIT_STATES:
        return "wait"
    if state in RUNNING_STATES:
        return "running"
    if state in TERMINAL_STATES:
        return "history"
    return None


def create_task(user: UserRecord, payload: TaskCreateRequest, db: Session) -> TaskInfo:
    """提交单个任务并写入数据库，初始进入等待区或挂起状态。"""
    task = create_task_model(user, payload, db)
    db.commit()
    db.refresh(task)
    record_audit(user.id, "task.create", "task", task.task_id, detail_json={"owner_user_id": task.user_id})
    return build_task_info(task, db)


def create_tasks_batch(user: UserRecord, payload: TaskBatchCreateRequest, db: Session) -> TaskBatchCreateResult:
    """按行批量提交任务，忽略空行、整行注释和 # 后面的注释内容。"""
    commands = parse_batch_commands(payload.commands)
    if not commands:
        raise validation_error("batch commands are empty")
    tasks: list[Task] = []
    for command in commands:
        task_payload = TaskCreateRequest(
            description=payload.description,
            env_id=payload.env_id,
            workdir=payload.workdir,
            command=command,
            priority=payload.priority,
            urgent=payload.urgent,
            on_hold=payload.on_hold,
            predecessor_task_id=payload.predecessor_task_id,
            requirement=payload.requirement,
        )
        tasks.append(create_task_model(user, task_payload, db, flush_only=True))
    db.commit()
    for task in tasks:
        db.refresh(task)
    record_audit(user.id, "task.batch_create", "task", "batch", detail_json={"count": len(tasks)})
    return TaskBatchCreateResult(items=[build_task_info(task, db) for task in tasks], total=len(tasks))


def get_task_for_user(user: UserRecord, task_id: str, db: Session) -> TaskInfo:
    """获取用户可见任务详情；不可见时按不存在处理，减少越权探测面。"""
    task = find_task_model(task_id, db)
    if task is None or not can_view_task(user, task, db):
        raise not_found("task not found")
    return build_task_info(task, db)


def update_task(user: UserRecord, task_id: str, payload: TaskUpdateRequest, db: Session) -> TaskInfo:
    """编辑等待、挂起或历史任务；历史任务编辑后会重新进入等待/挂起区。"""
    task = require_task_model_for_user(user, task_id, db)
    require_task_manager(user, task, db)
    if task.state in RUNNING_STATES:
        raise validation_error("running task cannot be edited")
    data = payload.model_dump(exclude_unset=True)
    owner = require_user_model(task.user_id, db)
    if "workdir" in data and data["workdir"] is not None:
        resolve_user_visible_path(data["workdir"], owner.username, owner.role)
    if "env_id" in data and data["env_id"] is not None:
        env = get_env_for_user(user, data["env_id"])
        if env.state not in {"available", "registered"}:
            raise validation_error("environment is not available")
    if "requirement" in data and payload.requirement is not None:
        validate_requirement_node(user, payload.requirement, db)
        apply_requirement(task, payload.requirement)
        data.pop("requirement")
    predecessor_changed = "predecessor_task_id" in data
    predecessor_task_id = data.pop("predecessor_task_id", None)
    for key, value in data.items():
        if hasattr(task, key):
            setattr(task, key, value)
    if "urgent" in data and task.urgent and "priority" not in data:
        task.priority = max(task.priority, 100)
    if predecessor_changed:
        replace_task_dependency(task, predecessor_task_id, user, db)
    if task.state in TERMINAL_STATES:
        # 历史任务被修改时按 2.0 行为重新入队；同时清理运行期字段，避免旧结果污染新执行。
        release_task_allocations(task.id, db)
        task.started_at = None
        task.finished_at = None
        task.return_code = None
        task.last_block_reason = ""
        task.generated_command = ""
    if "on_hold" in data or task.state in TERMINAL_STATES:
        task.state = "on_hold" if task.on_hold else "wait"
    add_task_event(db, task, "updated", "任务配置已修改", user.id, data)
    db.commit()
    db.refresh(task)
    record_audit(user.id, "task.update", "task", task.task_id, detail_json=data)
    return build_task_info(task, db)


def hold_task(user: UserRecord, task_id: str, db: Session) -> TaskInfo:
    """在等待和挂起之间切换，避免前端维护两个几乎相同的状态接口。"""
    task = require_task_model_for_user(user, task_id, db)
    require_task_manager(user, task, db)
    if task.state not in WAIT_STATES:
        raise validation_error("only waiting or held task can toggle hold")
    if task.state == "on_hold" or task.on_hold:
        task.state = "wait"
        task.on_hold = False
        action = "resumed"
        message = "任务已取消挂起"
        audit_action = "task.resume"
    else:
        task.state = "on_hold"
        task.on_hold = True
        action = "held"
        message = "任务已挂起"
        audit_action = "task.hold"
    add_task_event(db, task, action, message, user.id)
    db.commit()
    db.refresh(task)
    record_audit(user.id, audit_action, "task", task.task_id)
    return build_task_info(task, db)


def cancel_task(user: UserRecord, task_id: str, db: Session) -> TaskInfo:
    """中止任务；运行中任务会由执行 worker 根据运行时记录继续回收远端进程。"""
    task = require_task_model_for_user(user, task_id, db)
    require_task_manager(user, task, db)
    if task.state in TERMINAL_STATES:
        raise validation_error("finished task cannot be cancelled")
    previous_state = task.state
    task.state = "cancelled"
    task.on_hold = False
    task.finished_at = local_datetime()
    if previous_state not in {"starting", "running"}:
        release_task_allocations(task.id, db)
    append_task_log(task, "\nProgram Terminated By User\n")
    add_task_event(db, task, "cancelled", "任务已请求中止", user.id, {"previous_state": previous_state})
    db.commit()
    db.refresh(task)
    record_audit(user.id, "task.cancel", "task", task.task_id, detail_json={"previous_state": previous_state})
    return build_task_info(task, db)


def delete_task(user: UserRecord, task_id: str, delete_successors: bool, db: Session) -> TaskDeleteResult:
    """删除任务记录，可选择递归删除所有后继任务；运行中任务不会被删除。"""
    task = require_task_model_for_user(user, task_id, db)
    require_task_manager(user, task, db)
    successor_tasks = load_successor_tasks(task.id, db)
    selected = [task, *(successor_tasks if delete_successors else [])]
    selected_by_id = {item.id: item for item in selected}
    skipped = [item.task_id for item in selected if item.state in RUNNING_STATES]
    if task.state in RUNNING_STATES:
        raise validation_error("running task cannot be deleted")
    removed: list[str] = []
    for item in list(selected_by_id.values()):
        if item.state in RUNNING_STATES:
            continue
        require_task_manager(user, item, db)
        release_task_allocations(item.id, db)
        db.query(TaskDependency).filter(
            or_(TaskDependency.task_id == item.id, TaskDependency.prev_task_id == item.id)
        ).delete(synchronize_session=False)
        removed.append(item.task_id)
        db.delete(item)
    db.commit()
    record_audit(
        user.id,
        "task.delete",
        "task",
        task_id,
        detail_json={"removed": removed, "skipped_running": skipped, "delete_successors": delete_successors},
    )
    return TaskDeleteResult(removed=removed, skipped_running=skipped)


def preview_task_delete(user: UserRecord, task_id: str, db: Session) -> TaskDeletePreview:
    """返回递归后继任务 ID，用于删除前二次确认。"""
    task = require_task_model_for_user(user, task_id, db)
    require_task_manager(user, task, db)
    successors = [item.task_id for item in load_successor_tasks(task.id, db) if can_view_task(user, item, db)]
    return TaskDeletePreview(task_id=task.task_id, successors=successors)


def resubmit_task(user: UserRecord, task_id: str, db: Session) -> TaskInfo:
    """基于已有任务生成新 ID 的任务，保留命令、环境、资源需求和前驱配置。"""
    source = require_task_model_for_user(user, task_id, db)
    require_task_manager(user, source, db)
    predecessor = load_predecessor(source.id, db)
    payload = TaskCreateRequest(
        description=source.description,
        env_id=source.env_id,
        workdir=source.workdir,
        command=source.command,
        priority=source.priority,
        urgent=source.urgent,
        on_hold=False,
        predecessor_task_id=predecessor.task_id if predecessor else None,
        requirement=task_requirement_schema(source.requirement),
    )
    task = create_task_model(user, payload, db, owner_user_id=source.user_id)
    db.commit()
    db.refresh(task)
    record_audit(user.id, "task.resubmit", "task", task.task_id, detail_json={"source": task_id})
    return build_task_info(task, db)


def get_task_log(user: UserRecord, task_id: str, tail: str | None, db: Session) -> str:
    """读取任务日志尾部，避免大日志一次性进入内存。"""
    task = require_task_model_for_user(user, task_id, db)
    log_path = task.log_path or str(Path(get_settings().task_log_root) / f"{task.task_id}.log")
    path = Path(log_path)
    if not path.exists() or not path.is_file():
        return f"[{task.task_id}] 日志尚未生成\nlog_path={log_path}\n"
    max_bytes = parse_tail_bytes(tail)
    with path.open("rb") as file:
        file.seek(0, 2)
        size = file.tell()
        file.seek(max(0, size - max_bytes))
        return file.read(max_bytes).decode("utf-8", errors="replace")


def get_task_guard(user: UserRecord, task_id: str, db: Session) -> TaskGuardInfo:
    """返回运行时守护摘要，普通用户也只能访问自己可见的任务。"""
    task = require_task_model_for_user(user, task_id, db)
    guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
    allocations = db.scalars(select(TaskAllocation).where(TaskAllocation.task_id == task.id)).all()
    allocated_gpu_ids: list[int] = []
    for allocation in allocations:
        allocated_gpu_ids.extend(coerce_int(gpu_id) for gpu_id in allocation.gpu_ids)
    return TaskGuardInfo(
        task_id=task.task_id,
        root_pid=guard.root_pid if guard else None,
        process_group_id=guard.process_group_id if guard else None,
        allocated_gpu_ids=allocated_gpu_ids,
        observed_gpu_uuids=guard.observed_gpu_uuids if guard else [],
        violation_count=guard.violation_count if guard else 0,
        state=guard.state if guard else "not_started",
        last_check_at=datetime_to_iso(guard.last_check_at) if guard and guard.last_check_at else None,
    )


def create_task_model(
    user: UserRecord,
    payload: TaskCreateRequest,
    db: Session,
    owner_user_id: int | None = None,
    flush_only: bool = False,
) -> Task:
    """创建任务 ORM 对象；批量提交复用该函数以保证校验和事件一致。"""
    require_permission(user.role, "tasks:create")
    if user.state != "enabled":
        raise forbidden("disabled user cannot submit tasks")
    owner = require_user_model(owner_user_id or user.id, db)
    resolve_user_visible_path(payload.workdir, owner.username, owner.role)
    if payload.env_id is not None:
        env = get_env_for_user(user, payload.env_id)
        if env.state not in {"available", "registered"}:
            raise validation_error("environment is not available")
    validate_requirement_node(user, payload.requirement, db)
    task = Task(
        task_id=generate_task_id(db),
        user_id=owner.id,
        description=payload.description,
        env_id=payload.env_id,
        workdir=payload.workdir,
        command=payload.command,
        state="on_hold" if payload.on_hold else "wait",
        priority=max(payload.priority, 100 if payload.urgent else payload.priority),
        urgent=payload.urgent,
        on_hold=payload.on_hold,
        log_path=str(Path(get_settings().task_log_root) / f"pending-{int(time.time() * 1000)}.log"),
        created_at=local_datetime(),
        requirement=TaskRequirementModel(
            need_gpus=payload.requirement.need_gpus,
            gpu_types=payload.requirement.gpu_types,
            node_id=payload.requirement.node_id,
            allow_gpu_reuse=payload.requirement.allow_gpu_reuse,
            max_reuse_count=payload.requirement.max_reuse_count,
        ),
    )
    db.add(task)
    db.flush()
    task.log_path = str(Path(get_settings().task_log_root) / f"{task.task_id}.log")
    replace_task_dependency(task, payload.predecessor_task_id, user, db)
    add_task_event(db, task, "created", "任务已提交", user.id)
    if not flush_only:
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise validation_error("task id already exists") from exc
    return task


def apply_requirement(task: Task, requirement: TaskRequirement) -> None:
    """把资源需求写回 ORM；缺失旧记录时补建，避免历史数据升级后编辑失败。"""
    if task.requirement is None:
        task.requirement = TaskRequirementModel(task_id=task.id)
    task.requirement.need_gpus = requirement.need_gpus
    task.requirement.gpu_types = requirement.gpu_types
    task.requirement.node_id = requirement.node_id
    task.requirement.allow_gpu_reuse = requirement.allow_gpu_reuse
    task.requirement.max_reuse_count = requirement.max_reuse_count


def validate_requirement_node(user: UserRecord, requirement: TaskRequirement, db: Session) -> None:
    """校验指定节点必须存在且对当前用户可用，防止前端伪造私有节点 ID。"""
    if requirement.node_id is None:
        return
    node = db.get(Node, requirement.node_id)
    if node is None:
        raise validation_error("node not found")
    if user.role != Role.ADMIN and not can_user_access_node(user, node, db):
        raise forbidden("node is not visible to current user")


def replace_task_dependency(task: Task, predecessor_task_id: str | None, user: UserRecord, db: Session) -> None:
    """整体替换任务前驱关系，当前 UI 只支持一个前驱任务。"""
    db.query(TaskDependency).filter(TaskDependency.task_id == task.id).delete(synchronize_session=False)
    predecessor_task_id = (predecessor_task_id or "").strip()
    if not predecessor_task_id:
        return
    predecessor = find_task_model(predecessor_task_id, db)
    if predecessor is None or not can_view_task(user, predecessor, db):
        raise validation_error("predecessor task not found")
    if predecessor.id == task.id:
        raise validation_error("task cannot depend on itself")
    if predecessor.state in TERMINAL_STATES:
        # 历史前驱允许用于重跑场景；等待/执行前驱是常规路径，因此这里只禁止不存在和自依赖。
        pass
    db.add(TaskDependency(task_id=task.id, prev_task_id=predecessor.id, policy="success"))


def task_visibility_condition(user: UserRecord, db: Session):
    """生成任务可见性条件：学生看自己，导师看自己和学生，管理员看全部。"""
    if user.role == Role.ADMIN:
        return True
    visible_user_ids = [user.id]
    if user.role == Role.MENTOR:
        visible_user_ids.extend(
            db.scalars(select(UserSupervisor.student_id).where(UserSupervisor.supervisor_id == user.id)).all()
        )
    return Task.user_id.in_(visible_user_ids)


def task_state_condition(zone: str):
    """把等待区/执行区/历史区映射为任务状态集合，也兼容精确状态筛选。"""
    if not zone:
        return None
    if zone in {"wait", "waiting", "queue"}:
        return Task.state.in_(WAIT_STATES)
    if zone in {"running", "exec"}:
        return Task.state.in_(RUNNING_STATES)
    if zone in {"history", "hist", "finished"}:
        return Task.state.in_(TERMINAL_STATES)
    return Task.state == zone


def order_task_query(statement, zone: str):
    """不同区域使用不同排序：等待区按调度优先级，历史区最新结束在前。"""
    if zone in {"history", "hist", "finished"}:
        return statement.order_by(Task.finished_at.desc().nullslast(), Task.created_at.desc())
    if zone in {"wait", "waiting", "queue"}:
        return statement.order_by(Task.urgent.desc(), Task.priority.desc(), Task.created_at.asc())
    return statement.order_by(Task.started_at.desc().nullslast(), Task.created_at.desc())


def can_view_task(user: UserRecord, task: Task, db: Session) -> bool:
    """判断用户是否可见任务；导师范围来自 user_supervisors 关系表。"""
    if user.role == Role.ADMIN:
        return True
    if task.user_id == user.id:
        return True
    if user.role != Role.MENTOR:
        return False
    return db.scalar(
        select(UserSupervisor.id)
        .where(UserSupervisor.supervisor_id == user.id)
        .where(UserSupervisor.student_id == task.user_id)
    ) is not None


def can_manage_task(user: UserRecord, task: Task, db: Session) -> bool:
    """判断用户是否可管理任务：本人、导师名下学生或管理员。"""
    return task.user_id == user.id or (can_view_task(user, task, db) and user.role in {Role.ADMIN, Role.MENTOR})


def require_task_manager(user: UserRecord, task: Task, db: Session) -> None:
    """服务层强制任务管理权限，前端按钮禁用不能替代这里的判断。"""
    if not can_manage_task(user, task, db):
        raise forbidden("task manager required")


def require_task_model_for_user(user: UserRecord, task_id: str, db: Session) -> Task:
    """读取可见任务 ORM 对象，供会修改状态的服务函数使用。"""
    task = find_task_model(task_id, db)
    if task is None or not can_view_task(user, task, db):
        raise not_found("task not found")
    return task


def find_task_model(task_id: str, db: Session) -> Task | None:
    """按业务任务号查找任务，并预加载资源需求。"""
    return db.scalar(
        select(Task)
        .options(selectinload(Task.requirement))
        .where(Task.task_id == task_id)
    )


def require_user_model(user_id: int, db: Session) -> User:
    """获取任务所有人，缺失说明历史数据损坏，应按业务校验错误处理。"""
    user = db.get(User, user_id)
    if user is None:
        raise validation_error("task owner not found")
    return user


def build_task_infos(tasks: list[Task], db: Session) -> list[TaskInfo]:
    """批量构建任务列表响应，避免列表页按每条任务反复查询关联摘要。"""
    if not tasks:
        return []
    task_pks = [task.id for task in tasks]
    owner_ids = {task.user_id for task in tasks}
    env_ids = {task.env_id for task in tasks if task.env_id is not None}
    owners = {
        owner.id: owner
        for owner in db.scalars(select(User).where(User.id.in_(owner_ids))).all()
    } if owner_ids else {}
    envs = {
        env.id: env
        for env in db.scalars(select(Env).where(Env.id.in_(env_ids))).all()
    } if env_ids else {}
    dependencies = db.scalars(select(TaskDependency).where(TaskDependency.task_id.in_(task_pks))).all()
    predecessor_ids = {dependency.prev_task_id for dependency in dependencies}
    predecessors = {
        predecessor.id: predecessor
        for predecessor in db.scalars(select(Task).where(Task.id.in_(predecessor_ids))).all()
    } if predecessor_ids else {}
    predecessor_by_task_id = {
        dependency.task_id: predecessors.get(dependency.prev_task_id)
        for dependency in dependencies
    }
    allocation_task_ids = [
        task.id
        for task in tasks
        if task.state in RUNNING_STATES or task.state in TERMINAL_STATES
    ]
    allocations = load_latest_allocations(allocation_task_ids, db)
    node_ids = {allocation.node_id for allocation in allocations.values()}
    for task in tasks:
        if task.id not in allocations and task.requirement and task.requirement.node_id is not None:
            node_ids.add(task.requirement.node_id)
    nodes = {
        node.id: node
        for node in db.scalars(select(Node).where(Node.id.in_(node_ids))).all()
    } if node_ids else {}
    gpu_ids = {
        coerce_int(gpu_id)
        for allocation in allocations.values()
        for gpu_id in (allocation.gpu_ids or [])
        if coerce_int(gpu_id) > 0
    }
    gpus = {
        gpu.id: gpu
        for gpu in db.scalars(select(Gpu).where(Gpu.id.in_(gpu_ids))).all()
    } if gpu_ids else {}
    result: list[TaskInfo] = []
    for task in tasks:
        owner = owners.get(task.user_id)
        env = envs.get(task.env_id) if task.env_id is not None else None
        predecessor = predecessor_by_task_id.get(task.id)
        allocation = allocations.get(task.id)
        node = nodes.get(allocation.node_id) if allocation is not None else None
        gpu_indices: list[int] = []
        gpu_models: list[str] = []
        if allocation is not None:
            for gpu_id in allocation.gpu_ids or []:
                gpu = gpus.get(coerce_int(gpu_id))
                if gpu is None:
                    continue
                gpu_indices.append(gpu.gpu_index)
                if gpu.model not in gpu_models:
                    gpu_models.append(gpu.model)
        elif task.requirement and task.requirement.node_id is not None:
            node = nodes.get(task.requirement.node_id)
        result.append(TaskInfo(
            id=task.id,
            task_id=task.task_id,
            user_id=task.user_id,
            owner_name=owner.real_name if owner else "",
            owner_username=owner.username if owner else "",
            description=task.description,
            env_id=task.env_id,
            env_name=env.name if env else None,
            env_path=env.path if env else None,
            workdir=task.workdir,
            command=task.command,
            state=task.state,
            priority=task.priority,
            urgent=task.urgent,
            on_hold=task.on_hold,
            last_block_reason=task.last_block_reason,
            log_path=task.log_path,
            return_code=task.return_code,
            created_at=datetime_to_iso(task.created_at),
            started_at=datetime_to_iso(task.started_at) if task.started_at else None,
            finished_at=datetime_to_iso(task.finished_at) if task.finished_at else None,
            predecessor_task_id=predecessor.task_id if predecessor else None,
            predecessor_task_no=predecessor.task_id if predecessor else None,
            node_id=node.id if node else None,
            node_name=node.name if node else None,
            gpu_indices=gpu_indices,
            gpu_models=gpu_models,
            requirement=task_requirement_schema(task.requirement),
        ))
    return result


def build_task_info(task: Task, db: Session) -> TaskInfo:
    """把任务 ORM 记录转换为前端展示模型，并附带 owner/env/allocation 摘要。"""
    owner = db.get(User, task.user_id)
    env = db.get(Env, task.env_id) if task.env_id is not None else None
    predecessor = load_predecessor(task.id, db)
    # 等待/挂起任务展示的是用户当前提交的资源需求；历史 allocation 只用于运行中和历史任务，
    # 否则修改等待任务后列表会继续显示旧节点/旧 GPU，造成“保存失败”的错觉。
    allocation = load_latest_allocation(task.id, db) if task.state in RUNNING_STATES or task.state in TERMINAL_STATES else None
    node = None
    gpu_indices: list[int] = []
    gpu_models: list[str] = []
    if allocation is not None:
        node = db.get(Node, allocation.node_id)
        gpus = db.scalars(select(Gpu).where(Gpu.id.in_([coerce_int(item) for item in allocation.gpu_ids]))).all() if allocation.gpu_ids else []
        gpus_by_id = {gpu.id: gpu for gpu in gpus}
        for gpu_id in allocation.gpu_ids:
            gpu = gpus_by_id.get(coerce_int(gpu_id))
            if gpu is None:
                continue
            gpu_indices.append(gpu.gpu_index)
            if gpu.model not in gpu_models:
                gpu_models.append(gpu.model)
    elif task.requirement and task.requirement.node_id is not None:
        node = db.get(Node, task.requirement.node_id)
    return TaskInfo(
        id=task.id,
        task_id=task.task_id,
        user_id=task.user_id,
        owner_name=owner.real_name if owner else "",
        owner_username=owner.username if owner else "",
        description=task.description,
        env_id=task.env_id,
        env_name=env.name if env else None,
        env_path=env.path if env else None,
        workdir=task.workdir,
        command=task.command,
        state=task.state,
        priority=task.priority,
        urgent=task.urgent,
        on_hold=task.on_hold,
        last_block_reason=task.last_block_reason,
        log_path=task.log_path,
        return_code=task.return_code,
        created_at=datetime_to_iso(task.created_at),
        started_at=datetime_to_iso(task.started_at) if task.started_at else None,
        finished_at=datetime_to_iso(task.finished_at) if task.finished_at else None,
        predecessor_task_id=predecessor.task_id if predecessor else None,
        predecessor_task_no=predecessor.task_id if predecessor else None,
        node_id=node.id if node else None,
        node_name=node.name if node else None,
        gpu_indices=gpu_indices,
        gpu_models=gpu_models,
        requirement=task_requirement_schema(task.requirement),
    )


def task_requirement_schema(requirement: TaskRequirementModel | None) -> TaskRequirement:
    """把 ORM 资源需求转换为 Pydantic 模型，兼容缺失需求的历史任务。"""
    if requirement is None:
        return TaskRequirement()
    return TaskRequirement(
        need_gpus=requirement.need_gpus,
        gpu_types=requirement.gpu_types or [],
        node_id=requirement.node_id,
        allow_gpu_reuse=requirement.allow_gpu_reuse,
        max_reuse_count=requirement.max_reuse_count,
    )


def load_predecessor(task_pk: int, db: Session) -> Task | None:
    """读取当前任务的单前驱任务。"""
    dependency = db.scalar(select(TaskDependency).where(TaskDependency.task_id == task_pk))
    return db.get(Task, dependency.prev_task_id) if dependency else None


def load_successor_tasks(task_pk: int, db: Session) -> list[Task]:
    """递归读取所有后继任务，删除确认时需要展示完整影响范围。"""
    seen: set[int] = set()
    result: list[Task] = []
    frontier = [task_pk]
    while frontier:
        current = frontier.pop(0)
        rows = db.scalars(select(TaskDependency).where(TaskDependency.prev_task_id == current)).all()
        for row in rows:
            if row.task_id in seen:
                continue
            seen.add(row.task_id)
            task = db.get(Task, row.task_id)
            if task is None:
                continue
            result.append(task)
            frontier.append(task.id)
    return result


def load_latest_allocation(task_pk: int, db: Session) -> TaskAllocation | None:
    """读取任务最近一次分配，历史区也要展示实际执行节点和 GPU。"""
    return load_latest_allocations([task_pk], db).get(task_pk)


def load_latest_allocations(task_pks: list[int], db: Session) -> dict[int, TaskAllocation]:
    """一次取回每个任务最近一次 allocation，避免历史区列表逐条排序查询。"""
    if not task_pks:
        return {}
    ranked_allocations = (
        select(
            TaskAllocation.id.label("id"),
            TaskAllocation.task_id.label("task_id"),
            func.row_number().over(
                partition_by=TaskAllocation.task_id,
                order_by=(TaskAllocation.allocated_at.desc(), TaskAllocation.id.desc()),
            ).label("row_rank"),
        )
        .where(TaskAllocation.task_id.in_(task_pks))
        .subquery()
    )
    allocations = db.scalars(
        select(TaskAllocation)
        .join(ranked_allocations, TaskAllocation.id == ranked_allocations.c.id)
        .where(ranked_allocations.c.row_rank == 1)
    ).all()
    return {allocation.task_id: allocation for allocation in allocations}


def release_task_allocations(task_pk: int, db: Session) -> None:
    """释放任务尚未释放的 allocation，保证调度器后续能重新使用资源。"""
    now = local_datetime()
    allocations = db.scalars(
        select(TaskAllocation)
        .where(TaskAllocation.task_id == task_pk)
        .where(TaskAllocation.released_at.is_(None))
    ).all()
    for allocation in allocations:
        allocation.released_at = now


def add_task_event(
    db: Session,
    task: Task,
    event_type: str,
    message: str,
    actor_user_id: int | None = None,
    detail_json: dict | None = None,
) -> None:
    """追加任务事件流，记录状态变化原因，便于长时间运行后的审计和排障。"""
    db.add(TaskEvent(
        task_id=task.id,
        event_type=event_type,
        message=message,
        actor_user_id=actor_user_id,
        detail_json=detail_json or {},
    ))


def append_task_log(task: Task, text: str) -> None:
    """在任务日志存在或可创建时追加系统消息，失败不影响状态落库。"""
    log_path = task.log_path or str(Path(get_settings().task_log_root) / f"{task.task_id}.log")
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(text)
    except OSError:
        return


def parse_batch_commands(commands: str) -> list[str]:
    """清理批量命令：空行、注释行和 # 后文本都会被忽略。"""
    result: list[str] = []
    for raw_line in commands.splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip()
        if line:
            result.append(line)
    return result


def parse_tail_bytes(value: str | None) -> int:
    """解析日志 tail 大小，默认读取 200KB，最大限制 512KB 保护 API 内存。"""
    if not value:
        return 200 * 1024
    text = value.strip().lower()
    multiplier = 1
    if text.endswith("kb"):
        multiplier = 1024
        text = text[:-2]
    elif text.endswith("mb"):
        multiplier = 1024 * 1024
        text = text[:-2]
    try:
        size = int(float(text) * multiplier)
    except ValueError:
        size = 200 * 1024
    return max(1024, min(size, 512 * 1024))


def generate_task_id(db: Session) -> str:
    """生成毫秒精度时间戳任务号；同毫秒冲突时等待下一毫秒，避免覆盖旧任务。"""
    for _ in range(1000):
        now = local_datetime()
        candidate = now.strftime("%y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"
        if db.scalar(select(Task.id).where(Task.task_id == candidate)) is None:
            return candidate
        time.sleep(0.001)
    raise validation_error("failed to generate task id")


def datetime_to_iso(value) -> str:
    """把数据库时间转换为本地时区 ISO 字符串。"""
    converted = ensure_local_datetime(value)
    return converted.isoformat() if converted else ""


def coerce_int(value) -> int:
    """把 JSON 中的 GPU ID 安全转换为整数，异常值按 0 处理。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
