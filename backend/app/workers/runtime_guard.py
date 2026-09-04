import json
import logging
import shlex
import subprocess
from collections import defaultdict, deque
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.time_utils import local_datetime
from app.db.models import Gpu, Node, Task, TaskAllocation, TaskRuntimeGuard
from app.db.session import SessionLocal
from app.services.task_service import ALLOC_ERROR_STOP_REASON_PREFIX, add_task_event
from app.workers.common import run_forever

logger = logging.getLogger(__name__)

ACTIVE_GUARD_STATES = {"allocated", "starting", "running", "violation_pending", "check_failed", "uuid_pending"}
GPU_APPS_MARKER = "__NEBULAGRID_GPU_APPS__"


def runtime_guard_tick() -> None:
    """扫描运行任务的 PID 树和 GPU UUID 使用情况，发现越权后提交两阶段停止意图。"""
    with SessionLocal() as db:
        guards = db.scalars(
            select(TaskRuntimeGuard)
            .join(Task, Task.id == TaskRuntimeGuard.task_id)
            .join(Node, Node.id == TaskRuntimeGuard.node_id)
            .where(TaskRuntimeGuard.state.in_(ACTIVE_GUARD_STATES))
            .where(TaskRuntimeGuard.root_pid.is_not(None))
            .where(Task.state.in_(("starting", "running")))
            # 优先从未检查和最久未检查的记录开始，固定 LIMIT 下也不会让第 51 条以后永久饥饿。
            .order_by(TaskRuntimeGuard.last_check_at.asc().nulls_first(), TaskRuntimeGuard.id)
            .limit(50)
        ).all()
        checked = 0
        for guard in guards:
            checked += inspect_guard(db, guard)
            db.commit()
        logger.info("runtime guard checked %s/%s active guard records", checked, len(guards))


def inspect_guard(db: Session, guard: TaskRuntimeGuard) -> int:
    """无锁采样远端后再按 task->guard 加锁落库，身份或连接不确定时绝不触发停止。"""
    task = db.get(Task, guard.task_id)
    node = db.get(Node, guard.node_id)
    if task is None or node is None or task.state not in {"starting", "running"}:
        return 0
    try:
        allocation = load_single_open_allocation(db, task.id)
        root_pid, process_group_id, process_start_time, boot_id = load_runtime_identity(task, allocation, guard)
    except RuntimeError as exc:
        mark_guard_check_failed(db, task.id, guard.id, str(exc))
        return 0
    allowed_uuids = load_allowed_gpu_uuids(db, guard)
    try:
        observed_uuids = collect_observed_gpu_uuids(
            node,
            root_pid,
            process_group_id,
            process_start_time,
            boot_id,
        )
    except Exception as exc:  # noqa: BLE001 - 节点 SSH/nvidia-smi 短暂抖动不应直接改变任务状态。
        logger.warning("runtime guard failed for task %s on node %s: %s", task.task_id, node.name, exc)
        mark_guard_check_failed(db, task.id, guard.id, str(exc))
        return 0

    # 外部 SSH 期间任务可能已结束或收到停止请求；加锁后必须重新验证当前 launch 和 PID。
    task = db.scalar(
        select(Task).where(Task.id == task.id).with_for_update().execution_options(populate_existing=True)
    )
    if task is None or task.state not in {"starting", "running"}:
        return 0
    current_guard = db.scalar(
        select(TaskRuntimeGuard)
        .where(TaskRuntimeGuard.id == guard.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    try:
        current_allocation = load_single_open_allocation(db, task.id)
    except RuntimeError:
        if current_guard is not None:
            current_guard.last_check_at = local_datetime()
            current_guard.state = "check_failed"
        return 0
    if (
        current_guard is None
        or current_allocation.id != allocation.id
        or int(current_guard.root_pid or 0) != root_pid
        or int(current_guard.process_group_id or 0) != process_group_id
    ):
        return 0
    try:
        current_identity = load_runtime_identity(task, current_allocation, current_guard)
    except RuntimeError:
        current_guard.last_check_at = local_datetime()
        current_guard.state = "check_failed"
        return 0
    if current_identity != (root_pid, process_group_id, process_start_time, boot_id):
        # 远端采样期间控制文件若被替换，不能把旧采样结果落到新的 launch 身上。
        current_guard.last_check_at = local_datetime()
        current_guard.state = "check_failed"
        return 0
    guard = current_guard
    guard.observed_gpu_uuids = sorted(observed_uuids)
    guard.last_check_at = local_datetime()
    if not observed_uuids:
        guard.state = "running"
        guard.violation_count = 0
        return 1
    if not allowed_uuids:
        if any(coerce_int(item) > 0 for item in guard.allocated_gpu_ids or []):
            # GPU 任务的清单尚未同步 UUID 时先不判越权；CPU-only 任务没有分配 GPU，
            # 一旦观察到 GPU UUID 就应继续按越权处理。
            guard.state = "uuid_pending"
            guard.violation_count = 0
            return 1
    unexpected = observed_uuids - allowed_uuids
    if not unexpected:
        guard.state = "running"
        guard.violation_count = 0
        return 1
    guard.violation_count += 1
    if guard.violation_count < 2:
        guard.state = "violation_pending"
        add_task_event(
            db,
            task,
            "guard_warning",
            "Runtime Guard 检测到一次 GPU UUID 越权，等待下一轮复核",
            detail_json={"unexpected_gpu_uuids": sorted(unexpected), "allowed_gpu_uuids": sorted(allowed_uuids)},
        )
        return 1
    begin_alloc_error_cleanup(db, task, allocation, node, guard, unexpected, allowed_uuids)
    return 1


def load_single_open_allocation(db: Session, task_pk: int) -> TaskAllocation:
    """Runtime Guard 只接受唯一的当前 allocation；缺失或重叠 launch 都按身份不明处理。"""
    allocations = db.scalars(
        select(TaskAllocation)
        .where(TaskAllocation.task_id == task_pk)
        .where(TaskAllocation.released_at.is_(None))
        .order_by(TaskAllocation.id.desc())
        .limit(2)
    ).all()
    if len(allocations) != 1:
        raise RuntimeError("任务缺少唯一的未释放 allocation")
    return allocations[0]


def load_runtime_identity(
    task: Task,
    allocation: TaskAllocation,
    guard: TaskRuntimeGuard,
) -> tuple[int, int, int, str]:
    """严格绑定 task、allocation、guard 和 runtime 控制文件，防止复用 PID 被当成当前任务。"""
    runtime_path = Path(get_settings().runtime_root) / "tasks" / f"{task.task_id}.json"
    try:
        payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("缺少可验证的 runtime 元数据") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("runtime 元数据格式无效")
    launch_id = strict_positive_int(payload.get("launch_id"))
    root_pid = strict_positive_int(payload.get("pid"))
    process_group_id = strict_positive_int(payload.get("pgid"))
    process_start_time = strict_positive_int(payload.get("process_start_time"))
    boot_id = payload.get("boot_id")
    if str(payload.get("task_id") or "") != task.task_id or launch_id != allocation.id:
        raise RuntimeError("runtime 元数据不属于当前 launch")
    if (
        root_pid <= 0
        or process_group_id <= 0
        or process_start_time <= 0
        or not isinstance(boot_id, str)
        or not boot_id.strip()
    ):
        raise RuntimeError("runtime 进程身份字段不完整")
    if root_pid != int(guard.root_pid or 0) or process_group_id != int(guard.process_group_id or 0):
        raise RuntimeError("runtime 进程身份与数据库 guard 不一致")
    return root_pid, process_group_id, process_start_time, boot_id.strip()


def mark_guard_check_failed(db: Session, task_pk: int, guard_pk: int, error: str) -> None:
    """身份或 SSH 检查失败只更新守护状态；若任务已停止则不覆盖停止流程设置的 guard 状态。"""
    task = db.scalar(
        select(Task).where(Task.id == task_pk).with_for_update().execution_options(populate_existing=True)
    )
    if task is None or task.state not in {"starting", "running"}:
        return
    guard = db.scalar(
        select(TaskRuntimeGuard)
        .where(TaskRuntimeGuard.id == guard_pk)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if guard is None:
        return
    guard.last_check_at = local_datetime()
    guard.state = "check_failed"
    logger.debug("runtime guard identity/status check deferred for task %s: %s", task.task_id, error)


def load_allowed_gpu_uuids(db: Session, guard: TaskRuntimeGuard) -> set[str]:
    """把 allocation 中的 GPU ID 转成稳定 UUID，GPU index 只作为展示和 CUDA_VISIBLE_DEVICES 使用。"""
    gpu_ids = [coerce_int(item) for item in guard.allocated_gpu_ids or []]
    if not gpu_ids:
        return set()
    gpus = db.scalars(select(Gpu).where(Gpu.id.in_(gpu_ids))).all()
    return {gpu.gpu_uuid for gpu in gpus if gpu.gpu_uuid}


def collect_observed_gpu_uuids(
    node: Node,
    root_pid: int,
    process_group_id: int,
    process_start_time: int,
    boot_id: str,
) -> set[str]:
    """在远端采样前后核对 /proc 身份，再返回当前 launch 的 PID 树实际使用的 GPU UUID。"""
    remote_command = f"""
root={int(root_pid)}
expected_pgid={int(process_group_id)}
expected_start={int(process_start_time)}
expected_boot={shlex.quote(boot_id)}
verify_identity() {{
  current_boot="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)" || return 1
  [ -n "$expected_boot" ] && [ "$current_boot" = "$expected_boot" ] || return 1
  [ -r "/proc/$root/stat" ] || return 1
  stat_line="$(cat "/proc/$root/stat" 2>/dev/null)" || return 1
  stat_fields="${{stat_line##*) }}"
  set -- $stat_fields
  [ "$3" = "$expected_pgid" ] && [ "$4" = "$root" ] && [ "${{20}}" = "$expected_start" ]
}}
verify_identity || exit 76
ps -eo pid=,ppid= || exit 77
printf '\\n{GPU_APPS_MARKER}\\n'
nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits 2>/dev/null || exit 78
verify_identity || exit 76
"""
    settings = get_settings()
    output = subprocess.check_output(
        build_ssh_command(node, remote_command, settings.ssh_connect_timeout_seconds),
        text=True,
        stderr=subprocess.STDOUT,
        timeout=max(settings.ssh_operation_timeout_seconds, settings.ssh_connect_timeout_seconds + 5),
    )
    process_text, gpu_text = split_remote_runtime_output(output)
    task_pids = expand_pid_tree(root_pid, parse_process_table(process_text))
    return parse_gpu_apps(gpu_text, task_pids)


def split_remote_runtime_output(output: str) -> tuple[str, str]:
    """按固定 marker 拆分 ps 与 nvidia-smi 输出，远端命令空输出时保持可解析。"""
    if GPU_APPS_MARKER not in output:
        return output, ""
    process_text, gpu_text = output.split(GPU_APPS_MARKER, maxsplit=1)
    return process_text, gpu_text


def parse_process_table(text: str) -> dict[int, list[int]]:
    """解析 ps 的 pid/ppid 表，构造父子索引用于定位任务完整 PID 树。"""
    children_by_parent: dict[int, list[int]] = defaultdict(list)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        pid = coerce_int(parts[0])
        ppid = coerce_int(parts[1])
        if pid > 0:
            children_by_parent[ppid].append(pid)
    return children_by_parent


def expand_pid_tree(root_pid: int, children_by_parent: dict[int, list[int]]) -> set[int]:
    """从 runner 记录的根 PID 展开所有子进程，覆盖 torchrun、shell wrapper 等派生进程。"""
    seen: set[int] = set()
    queue: deque[int] = deque([root_pid])
    while queue:
        pid = queue.popleft()
        if pid in seen:
            continue
        seen.add(pid)
        queue.extend(children_by_parent.get(pid, []))
    return seen


def parse_gpu_apps(text: str, task_pids: set[int]) -> set[str]:
    """解析 nvidia-smi compute-apps，只保留任务 PID 树实际使用的 GPU UUID。"""
    observed: set[str] = set()
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        pid = coerce_int(parts[0])
        gpu_uuid = parts[1]
        if pid in task_pids and gpu_uuid and gpu_uuid != "[Not Supported]":
            observed.add(gpu_uuid)
    return observed


def begin_alloc_error_cleanup(
    db: Session,
    task: Task,
    allocation: TaskAllocation,
    node: Node,
    guard: TaskRuntimeGuard,
    unexpected: set[str],
    allowed_uuids: set[str],
) -> None:
    """确认 GPU 越权后只写停止意图；executor 验证退出后才写 alloc_error 并释放资源。"""
    guard.state = "cancelling"
    task.state = "cancelling"
    task.finished_at = None
    task.return_code = None
    task.last_block_reason = f"{ALLOC_ERROR_STOP_REASON_PREFIX}；正在确认远端进程退出"
    add_task_event(
        db,
        task,
        "alloc_error_cancelling",
        "Runtime Guard 检测到 GPU 越权，任务进入停止中",
        detail_json={
            "node_id": node.id,
            "allocation_id": allocation.id,
            "unexpected_gpu_uuids": sorted(unexpected),
            "allowed_gpu_uuids": sorted(allowed_uuids),
            "root_pid": guard.root_pid,
            "process_group_id": guard.process_group_id,
        },
    )


def build_ssh_command(node: Node, remote_command: str, connect_timeout_seconds: int | None = None) -> list[str]:
    """构造统一 SSH 命令；远端命令已整体作为一个 shell 片段传入。"""
    timeout = connect_timeout_seconds or get_settings().ssh_connect_timeout_seconds
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(timeout))}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{node.ssh_user}@{node.ip}",
        remote_command,
    ]


def coerce_int(value) -> int:
    """安全转换整数，远端异常输出不会让守护进程退出。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def strict_positive_int(value) -> int:
    """进程控制字段只接受正整数或十进制字符串，拒绝布尔值和模糊转换。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str) and value.strip().isdecimal():
        converted = int(value.strip())
        return converted if converted > 0 else 0
    return 0


def main() -> None:
    """运行时守护 worker 入口，负责发现 GPU 越权并发起确认停止流程。"""
    run_forever("nebulagrid-guard", 5, runtime_guard_tick)


if __name__ == "__main__":
    main()
