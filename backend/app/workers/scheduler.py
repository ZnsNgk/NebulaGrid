import logging
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.time_utils import local_datetime
from app.db.models import Gpu, Node, Setting, Task, TaskAllocation, TaskDependency, TaskRuntimeGuard, User
from app.db.session import SessionLocal
from app.services.auth_service import user_model_to_record
from app.services.metrics_service import LatestMetrics, get_latest_metrics
from app.services.node_service import (
    can_user_access_node,
    is_control_plane_node,
    load_group_shared_user_ids,
    normalize_owner_ids,
)
from app.services.task_service import TERMINAL_STATES, add_task_event

logger = logging.getLogger(__name__)

DEFAULT_REUSE_FREE_VRAM_RATIO = 0.40
DEFAULT_EXCLUSIVE_USED_VRAM_RATIO = 0.20


def scheduler_tick() -> None:
    """扫描等待任务并做事务化资源分配，成功后任务进入 dispatching。"""
    with SessionLocal() as db:
        if not scheduler_enabled(db):
            logger.info("scheduler disabled")
            return
        if not acquire_scheduler_lock(db):
            logger.info("scheduler tick skipped because another instance holds the DB lock")
            return
        released = release_terminal_allocations(db)
        waiting_tasks = db.scalars(
            select(Task)
            .options(selectinload(Task.requirement))
            .where(Task.state == "wait")
            .order_by(Task.urgent.desc(), Task.priority.desc(), Task.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(50)
        ).all()
        scheduled = 0
        for task in waiting_tasks:
            if try_schedule_task(db, task):
                scheduled += 1
                # 每轮最多成功分配一个任务，确保下一轮基于已提交的 allocation 重新计算占用。
                break
        db.commit()
        logger.info(
            "scheduler released %s terminal allocations, observed %s waiting tasks, scheduled %s",
            released,
            len(waiting_tasks),
            scheduled,
        )


def acquire_scheduler_lock(db: Session) -> bool:
    """锁定调度器哨兵行，防止多实例用同一资源快照重复领取任务。"""
    key = "scheduler.instance_lock"
    if db.get(Setting, key) is None:
        db.add(Setting(key=key, value="locked-by-row-transaction"))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
        except OperationalError:
            db.rollback()
            return False
    try:
        db.execute(select(Setting).where(Setting.key == key).with_for_update(nowait=True)).scalar_one()
        return True
    except OperationalError:
        db.rollback()
        return False


def try_schedule_task(db: Session, task: Task) -> bool:
    """尝试调度单个任务；失败时只更新阻塞原因，等待下轮重试。"""
    owner = db.get(User, task.user_id)
    if owner is None or owner.state != "enabled":
        mark_blocked(db, task, "USER_DISABLED", "任务所有人已停用或不存在")
        return False
    if not dependency_satisfied(db, task):
        return False
    if task.requirement is None:
        mark_blocked(db, task, "MISSING_REQUIREMENT", "任务缺少资源需求")
        return False
    owner_record = user_model_to_record(owner, db)
    candidates = visible_schedulable_nodes(db, owner_record, task.requirement.node_id)
    if not candidates:
        mark_blocked(db, task, "NO_AVAILABLE_NODE", "没有可调度节点")
        return False
    if task.requirement.need_gpus <= 0:
        allocate_task(db, task, candidates[0], [], "cpu")
        return True
    gpu_ids = [gpu.id for node in candidates for gpu in node.gpus]
    metrics = load_gpu_metrics(gpu_ids)
    selected = select_gpu_allocation(db, candidates, task, metrics)
    if selected is None:
        mark_blocked(db, task, "RESOURCE_UNAVAILABLE", "没有满足 GPU 数量、型号或复用策略的资源")
        return False
    node, gpus, mode = selected
    allocate_task(db, task, node, gpus, mode)
    return True


def dependency_satisfied(db: Session, task: Task) -> bool:
    """检查前驱任务；前驱失败时当前任务进入 dependency_failed 终态。"""
    dependency = db.scalar(select(TaskDependency).where(TaskDependency.task_id == task.id))
    if dependency is None:
        return True
    predecessor = db.get(Task, dependency.prev_task_id)
    if predecessor is None:
        mark_blocked(db, task, "WAITING_DEPENDENCY", "前驱任务不存在")
        return False
    if predecessor.state == "succeeded":
        return True
    if predecessor.state in TERMINAL_STATES:
        task.state = "dependency_failed"
        task.finished_at = local_datetime()
        task.last_block_reason = f"前驱任务 {predecessor.task_id} 未成功完成"
        add_task_event(
            db,
            task,
            "dependency_failed",
            task.last_block_reason,
            detail_json={"predecessor": predecessor.task_id, "predecessor_state": predecessor.state},
        )
        return False
    mark_blocked(db, task, "WAITING_DEPENDENCY", f"等待前驱任务 {predecessor.task_id}")
    return False


def visible_schedulable_nodes(db: Session, user, requested_node_id: int | None) -> list[Node]:
    """按节点可见性、在线状态、调度开关和用户资源优先级筛出候选节点。"""
    nodes = db.scalars(select(Node).options(selectinload(Node.gpus)).order_by(Node.id)).all()
    candidates: list[Node] = []
    for node in nodes:
        if is_control_plane_node(node):
            continue
        if requested_node_id is not None and node.id != requested_node_id:
            continue
        if node.state != "online" or not node.scheduling_enabled:
            continue
        if user.role.value != "admin" and not can_user_access_node(user, node, db):
            continue
        candidates.append(node)
    return sorted(candidates, key=lambda node: (node_schedule_priority(db, user, node), node.id))


def node_schedule_priority(db: Session, user, node: Node) -> int:
    """给候选节点排序：自有节点、组内共享、组内他人公开私有节点、公开节点。"""
    owner_ids = normalize_owner_ids(node)
    if user.id in owner_ids:
        return 0
    sharing_scope = getattr(node, "sharing_scope", None) or ("public" if node.is_public else "none")
    access_scope = getattr(node, "access_scope", None) or ("public" if node.is_public else "private")
    group_visible = bool(owner_ids) and user.id in load_group_shared_user_ids(owner_ids, db)
    if sharing_scope == "group" and group_visible:
        return 1
    if access_scope == "private" and sharing_scope == "public" and group_visible:
        return 2
    return 3


def select_gpu_allocation(
    db: Session,
    nodes: list[Node],
    task: Task,
    metrics: LatestMetrics,
) -> tuple[Node, list[Gpu], str] | None:
    """在单个节点内选择满足数量、型号和复用策略的 GPU。"""
    occupancy = load_gpu_occupancy(db)
    required = max(1, task.requirement.need_gpus)
    allowed_models = {normalize_gpu_model(item) for item in task.requirement.gpu_types or []}
    allow_reuse = task.requirement.allow_gpu_reuse
    for node in nodes:
        usable: list[Gpu] = []
        for gpu in sorted(node.gpus, key=lambda item: item.gpu_index):
            if not gpu.schedulable:
                continue
            if allowed_models and normalize_gpu_model(gpu.model) not in allowed_models:
                continue
            if gpu_is_available(gpu, task, occupancy, metrics):
                usable.append(gpu)
            if len(usable) >= required:
                return node, usable[:required], "reuse" if allow_reuse else "exclusive"
    return None


def gpu_is_available(gpu: Gpu, task: Task, occupancy: dict[int, dict[str, int | bool]], metrics: LatestMetrics) -> bool:
    """判断单张 GPU 是否满足独占或复用条件，监控缺失时只按数据库 allocation 判断。"""
    record = occupancy.get(gpu.id, {"count": 0, "exclusive": False})
    count = int(record.get("count", 0))
    has_exclusive = bool(record.get("exclusive"))
    metric = metrics.gpus.get(gpu.id)
    if task.requirement.allow_gpu_reuse:
        if has_exclusive or count >= task.requirement.max_reuse_count:
            return False
        if metric and gpu.total_vram_mb > 0:
            return metric.free_vram_mb is not None and metric.free_vram_mb / gpu.total_vram_mb >= DEFAULT_REUSE_FREE_VRAM_RATIO
        return True
    if count > 0 or has_exclusive:
        return False
    if metric and gpu.total_vram_mb > 0 and metric.free_vram_mb is not None:
        used_ratio = 1 - (metric.free_vram_mb / gpu.total_vram_mb)
        return used_ratio <= DEFAULT_EXCLUSIVE_USED_VRAM_RATIO
    return True


def load_gpu_occupancy(db: Session) -> dict[int, dict[str, int | bool]]:
    """读取未释放 allocation，作为调度器的权威占用来源。"""
    allocations = db.scalars(select(TaskAllocation).where(TaskAllocation.released_at.is_(None))).all()
    occupancy: dict[int, dict[str, int | bool]] = {}
    for allocation in allocations:
        for gpu_id in allocation.gpu_ids or []:
            item = occupancy.setdefault(int(gpu_id), {"count": 0, "exclusive": False})
            item["count"] = int(item["count"]) + 1
            if allocation.allocation_mode == "exclusive":
                item["exclusive"] = True
    return occupancy


def allocate_task(db: Session, task: Task, node: Node, gpus: list[Gpu], mode: str) -> None:
    """写入 allocation、运行时守护记录和任务事件，并推进到派发状态。"""
    allocation = TaskAllocation(
        task_id=task.id,
        node_id=node.id,
        gpu_ids=[gpu.id for gpu in gpus],
        cpu_allocated=1 if mode == "cpu" else 0,
        allocation_mode=mode,
    )
    db.add(allocation)
    guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
    if guard is None:
        guard = TaskRuntimeGuard(
            task_id=task.id,
            node_id=node.id,
            allocated_gpu_ids=[gpu.id for gpu in gpus],
            state="allocated",
        )
        db.add(guard)
    else:
        guard.node_id = node.id
        guard.allocated_gpu_ids = [gpu.id for gpu in gpus]
        guard.state = "allocated"
    task.state = "dispatching"
    task.last_block_reason = ""
    task.generated_command = ""
    add_task_event(
        db,
        task,
        "allocated",
        "调度器已分配资源",
        detail_json={
            "node_id": node.id,
            "node_name": node.name,
            "gpu_ids": [gpu.id for gpu in gpus],
            "gpu_indices": [gpu.gpu_index for gpu in gpus],
            "allocation_mode": mode,
        },
    )
    db.flush()


def release_terminal_allocations(db: Session) -> int:
    """释放已进入终态任务的未释放 allocation，调度器每轮先清理再分配新任务。"""
    allocations = db.scalars(
        select(TaskAllocation)
        .join(Task, TaskAllocation.task_id == Task.id)
        .where(TaskAllocation.released_at.is_(None))
        .where(Task.state.in_(TERMINAL_STATES))
    ).all()
    now = local_datetime()
    for allocation in allocations:
        allocation.released_at = now
    if allocations:
        db.flush()
    return len(allocations)


def mark_blocked(db: Session, task: Task, reason: str, message: str) -> None:
    """记录最近阻塞原因；只在原因变化时写事件，避免刷屏。"""
    if task.last_block_reason == message:
        return
    task.last_block_reason = message
    add_task_event(db, task, "blocked", message, detail_json={"reason": reason})


def load_gpu_metrics(gpu_ids: list[int]) -> LatestMetrics:
    """读取 InfluxDB GPU 最新指标；监控未配置时返回空快照，不阻断调度。"""
    try:
        return get_latest_metrics([], gpu_ids)
    except Exception:
        logger.exception("failed to load gpu metrics for scheduler")
        return LatestMetrics()


def normalize_gpu_model(value: str) -> str:
    """归一化 GPU 型号字符串，减少空格和大小写差异造成的匹配失败。"""
    return "".join(str(value or "").lower().split())


def scheduler_enabled(db: Session) -> bool:
    """读取调度器开关，缺失时默认启用以符合最小部署预期。"""
    setting = db.get(Setting, "scheduler.enabled")
    return setting is None or setting.value.lower() == "true"


def scheduler_interval_seconds(db: Session) -> int:
    """读取调度间隔；优先使用数据库设置，便于管理员后台在线调整。"""
    setting = db.get(Setting, "scheduler.interval_seconds")
    raw_value = setting.value if setting is not None else str(get_settings().scheduler_interval_seconds)
    try:
        return max(1, min(3600, int(raw_value)))
    except (TypeError, ValueError):
        return get_settings().scheduler_interval_seconds


def main() -> None:
    """调度器命令行入口，供 systemd 使用 python -m app.workers.scheduler 启动。"""
    logger.info("nebulagrid-scheduler started")
    while True:
        interval_seconds = get_settings().scheduler_interval_seconds
        try:
            scheduler_tick()
            with SessionLocal() as db:
                interval_seconds = scheduler_interval_seconds(db)
        except Exception:
            logger.exception("nebulagrid-scheduler tick failed")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
