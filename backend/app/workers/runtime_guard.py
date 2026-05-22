import logging
import shlex
import subprocess
from collections import defaultdict, deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_utils import local_datetime
from app.db.models import Gpu, Node, Task, TaskRuntimeGuard
from app.db.session import SessionLocal
from app.services.task_service import add_task_event, append_task_log, release_task_allocations
from app.workers.common import run_forever

logger = logging.getLogger(__name__)

ACTIVE_GUARD_STATES = {"allocated", "starting", "running", "violation_pending", "check_failed", "uuid_pending"}
GPU_APPS_MARKER = "__NEBULAGRID_GPU_APPS__"


def runtime_guard_tick() -> None:
    """扫描运行任务的 PID 树和 GPU UUID 使用情况，发现越权后进入 alloc_error。"""
    with SessionLocal() as db:
        guards = db.scalars(
            select(TaskRuntimeGuard)
            .where(TaskRuntimeGuard.state.in_(ACTIVE_GUARD_STATES))
            .where(TaskRuntimeGuard.root_pid.is_not(None))
            .limit(50)
        ).all()
        checked = 0
        for guard in guards:
            checked += inspect_guard(db, guard)
            db.commit()
        logger.info("runtime guard checked %s/%s active guard records", checked, len(guards))


def inspect_guard(db: Session, guard: TaskRuntimeGuard) -> int:
    """检查单条守护记录；远端临时失败只记录日志，避免误杀正常任务。"""
    task = db.get(Task, guard.task_id)
    node = db.get(Node, guard.node_id)
    if task is None or node is None or task.state not in {"starting", "running"}:
        return 0
    allowed_uuids = load_allowed_gpu_uuids(db, guard)
    try:
        observed_uuids = collect_observed_gpu_uuids(node, int(guard.root_pid or 0))
    except Exception as exc:  # noqa: BLE001 - 节点 SSH/nvidia-smi 短暂抖动不应直接改变任务状态。
        logger.warning("runtime guard failed for task %s on node %s: %s", task.task_id, node.name, exc)
        guard.last_check_at = local_datetime()
        guard.state = "check_failed"
        return 0
    guard.observed_gpu_uuids = sorted(observed_uuids)
    guard.last_check_at = local_datetime()
    if not observed_uuids:
        guard.state = "running"
        return 1
    if not allowed_uuids:
        # 监控尚未同步 GPU UUID 时先不判越权，避免旧库升级后立刻误报。
        guard.state = "uuid_pending"
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
    mark_alloc_error(db, task, node, guard, unexpected, allowed_uuids)
    return 1


def load_allowed_gpu_uuids(db: Session, guard: TaskRuntimeGuard) -> set[str]:
    """把 allocation 中的 GPU ID 转成稳定 UUID，GPU index 只作为展示和 CUDA_VISIBLE_DEVICES 使用。"""
    gpu_ids = [coerce_int(item) for item in guard.allocated_gpu_ids or []]
    if not gpu_ids:
        return set()
    gpus = db.scalars(select(Gpu).where(Gpu.id.in_(gpu_ids))).all()
    return {gpu.gpu_uuid for gpu in gpus if gpu.gpu_uuid}


def collect_observed_gpu_uuids(node: Node, root_pid: int) -> set[str]:
    """在计算节点采集 PID 树和 nvidia-smi compute-apps，并返回该任务实际占用的 GPU UUID。"""
    if root_pid <= 0:
        return set()
    remote_command = (
        "ps -eo pid=,ppid=; "
        f"printf '\\n{GPU_APPS_MARKER}\\n'; "
        "nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits 2>/dev/null || true"
    )
    output = subprocess.check_output(build_ssh_command(node, remote_command), text=True, stderr=subprocess.STDOUT, timeout=15)
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


def mark_alloc_error(
    db: Session,
    task: Task,
    node: Node,
    guard: TaskRuntimeGuard,
    unexpected: set[str],
    allowed_uuids: set[str],
) -> None:
    """确认越权后终止远端进程组、释放 allocation，并把任务置为 alloc_error 终态。"""
    try:
        terminate_remote_process(node, guard)
    except Exception as exc:  # noqa: BLE001 - alloc_error 必须落库，终止失败通过事件详情暴露给管理员。
        logger.warning("failed to terminate alloc-error task %s on %s: %s", task.task_id, node.name, exc)
    guard.state = "alloc_error"
    task.state = "alloc_error"
    task.finished_at = local_datetime()
    task.last_block_reason = "Runtime Guard 检测到任务使用未分配 GPU"
    release_task_allocations(task.id, db)
    append_task_log(
        task,
        "\nNebulaGrid Runtime Guard stopped task: unexpected GPU UUID "
        f"{', '.join(sorted(unexpected))}\n",
    )
    add_task_event(
        db,
        task,
        "alloc_error",
        "Runtime Guard 已中止使用未分配 GPU 的任务",
        detail_json={
            "node_id": node.id,
            "unexpected_gpu_uuids": sorted(unexpected),
            "allowed_gpu_uuids": sorted(allowed_uuids),
            "root_pid": guard.root_pid,
            "process_group_id": guard.process_group_id,
        },
    )


def terminate_remote_process(node: Node, guard: TaskRuntimeGuard) -> None:
    """优先按进程组终止任务，失败时退回 root PID，降低越权进程继续占卡的时间窗口。"""
    target = guard.process_group_id or guard.root_pid
    if target is None:
        return
    command = f"kill -TERM -{int(target)} 2>/dev/null || kill -TERM {int(target)} 2>/dev/null || true"
    subprocess.check_output(build_ssh_command(node, command), text=True, stderr=subprocess.STDOUT, timeout=10)


def build_ssh_command(node: Node, remote_command: str) -> list[str]:
    """构造统一 SSH 命令；远端命令已整体作为一个 shell 片段传入。"""
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
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


def main() -> None:
    """运行时守护 worker 入口，负责发现 GPU 越权并标记 alloc_error。"""
    run_forever("nebulagrid-guard", 5, runtime_guard_tick)


if __name__ == "__main__":
    main()
