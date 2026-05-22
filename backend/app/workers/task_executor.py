import json
import logging
import shlex
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.path_resolver import resolve_user_visible_path
from app.core.time_utils import local_datetime
from app.db.models import Env, Gpu, Node, Task, TaskAllocation, TaskRuntimeGuard, User
from app.db.session import SessionLocal
from app.services.task_service import add_task_event, append_task_log, release_task_allocations
from app.workers.common import run_forever

logger = logging.getLogger(__name__)


def task_executor_tick() -> None:
    """派发 dispatching 任务，并回收运行中任务的远端状态。"""
    settings = get_settings()
    with SessionLocal() as db:
        dispatching_tasks = db.scalars(select(Task).where(Task.state == "dispatching").limit(10)).all()
        for task in dispatching_tasks:
            start_remote_task(db, task, settings)
            db.commit()
        active_tasks = db.scalars(
            select(Task).where(Task.state.in_(("starting", "running", "cancelled"))).limit(50)
        ).all()
        for task in active_tasks:
            collect_remote_status(db, task, settings)
            db.commit()
        logger.info("executor handled dispatching=%s active=%s", len(dispatching_tasks), len(active_tasks))


def start_remote_task(db: Session, task: Task, settings: Settings) -> None:
    """通过 SSH 调用远端 runner 启动任务，并记录 PID、日志和执行时间。"""
    allocation = load_open_allocation(db, task.id)
    if allocation is None:
        finish_task(db, task, "failed", "任务缺少资源分配记录", return_code=None)
        return
    node = db.get(Node, allocation.node_id)
    owner = db.get(User, task.user_id)
    if node is None or owner is None:
        finish_task(db, task, "offline_error", "节点或用户记录不存在", return_code=None)
        release_task_allocations(task.id, db)
        return
    try:
        command = build_generated_command(task, db, settings)
        workdir = resolve_user_visible_path(task.workdir, owner.username, owner.role)
        cuda_indices = allocation_cuda_indices(db, allocation)
        runtime_path = runtime_metadata_path(settings, task.task_id)
        status_path = runtime_status_path(settings, task.task_id)
        output = run_remote_runner(
            node,
            settings,
            workdir=str(workdir).replace("\\", "/"),
            command=command,
            log_path=task.log_path,
            runtime_path=runtime_path,
            status_path=status_path,
            cuda_visible_devices=",".join(str(index) for index in cuda_indices),
        )
        metadata = json.loads(output)
    except Exception as exc:  # noqa: BLE001 - worker 必须把启动失败转为任务状态，而不是退出。
        logger.warning("failed to start task %s: %s", task.task_id, exc)
        append_task_log(task, f"\nNebulaGrid failed to start task: {exc}\n")
        finish_task(db, task, "offline_error", "SSH 启动任务失败", return_code=None)
        release_task_allocations(task.id, db)
        return
    guard = ensure_guard(db, task, allocation)
    guard.root_pid = coerce_int(metadata.get("pid")) or None
    guard.process_group_id = coerce_int(metadata.get("pgid")) or guard.root_pid
    guard.state = "running"
    task.generated_command = command
    task.state = "running"
    task.started_at = local_datetime()
    task.last_block_reason = ""
    add_task_event(
        db,
        task,
        "started",
        "远端任务已启动",
        detail_json={"node_id": node.id, "pid": guard.root_pid, "pgid": guard.process_group_id},
    )


def collect_remote_status(db: Session, task: Task, settings: Settings) -> None:
    """读取远端 runner 生成的状态文件，完成任务后释放资源。"""
    allocation = load_open_allocation(db, task.id)
    if allocation is None:
        return
    node = db.get(Node, allocation.node_id)
    if node is None:
        finish_task(db, task, "offline_error", "节点记录不存在", return_code=None)
        release_task_allocations(task.id, db)
        return
    if task.state == "cancelled":
        stop_remote_process(db, task, node)
        release_task_allocations(task.id, db)
        return
    try:
        status_text = read_remote_status(node, settings, runtime_status_path(settings, task.task_id))
    except Exception as exc:  # noqa: BLE001 - 在线节点短暂 SSH 抖动时保留运行状态。
        if node.state != "online":
            finish_task(db, task, "offline_error", "运行中节点掉线", return_code=None)
            release_task_allocations(task.id, db)
        else:
            logger.debug("task %s status not ready: %s", task.task_id, exc)
        return
    if not status_text.strip():
        return
    try:
        status = json.loads(status_text)
    except json.JSONDecodeError:
        logger.warning("task %s status file is not json: %s", task.task_id, status_text[:200])
        return
    return_code = coerce_int(status.get("return_code"))
    final_state = "succeeded" if return_code == 0 else "failed"
    finish_task(db, task, final_state, "任务执行结束", return_code=return_code)
    release_task_allocations(task.id, db)


def build_generated_command(task: Task, db: Session, settings: Settings) -> str:
    """生成统一执行前缀，用户命令只作为最后一段命令主体。"""
    env = db.get(Env, task.env_id) if task.env_id is not None else None
    activate = miniconda_activate_path(settings)
    parts = [
        "source ~/.bashrc",
        f"source {shlex.quote(activate)}",
    ]
    if env is not None:
        parts.append(f"conda activate {shlex.quote(env.name)}")
    parts.extend(
        [
            "export PYTHONUNBUFFERED=1",
            "export CUDA_DEVICE_ORDER=PCI_BUS_ID",
            "export NCCL_P2P_DISABLE=1",
            "export QT_QPA_PLATFORM=offscreen",
            task.command,
        ]
    )
    return " && ".join(parts)


def run_remote_runner(
    node: Node,
    settings: Settings,
    workdir: str,
    command: str,
    log_path: str,
    runtime_path: str,
    status_path: str,
    cuda_visible_devices: str,
) -> str:
    """在计算节点执行远端 runner.py，并返回其 JSON 元数据输出。"""
    remote_script = f"{settings.remote_code_root.rstrip('/')}/runner.py"
    remote_command = shlex.join(
        [
            settings.miniconda_python,
            remote_script,
            "--workdir",
            workdir,
            "--command",
            command,
            "--log-path",
            log_path,
            "--runtime-path",
            runtime_path,
            "--status-path",
            status_path,
            "--cuda-visible-devices",
            cuda_visible_devices,
        ]
    )
    ssh_command = [
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
    return subprocess.check_output(ssh_command, text=True, stderr=subprocess.STDOUT, timeout=20)


def read_remote_status(node: Node, settings: Settings, status_path: str) -> str:
    """读取远端状态文件；不存在时返回空文本。"""
    command = f"test -f {shlex.quote(status_path)} && cat {shlex.quote(status_path)} || true"
    ssh_command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{node.ssh_user}@{node.ip}",
        command,
    ]
    return subprocess.check_output(ssh_command, text=True, stderr=subprocess.STDOUT, timeout=10)


def stop_remote_process(db: Session, task: Task, node: Node) -> None:
    """中止远端进程组；失败时记录事件但保留 cancelled 终态。"""
    guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
    if guard is None or (guard.process_group_id is None and guard.root_pid is None):
        add_task_event(db, task, "cancelled", "任务已中止，但没有可回收的远端 PID")
        return
    target = guard.process_group_id or guard.root_pid
    command = f"kill -TERM -{int(target)} 2>/dev/null || kill -TERM {int(target)} 2>/dev/null || true"
    ssh_command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{node.ssh_user}@{node.ip}",
        command,
    ]
    try:
        subprocess.check_output(ssh_command, text=True, stderr=subprocess.STDOUT, timeout=10)
        guard.state = "cancelled"
        add_task_event(db, task, "cancelled", "远端任务进程已请求终止", detail_json={"target": target})
    except Exception as exc:  # noqa: BLE001 - 取消状态已经落库，回收失败通过事件追踪。
        guard.state = "cancel_failed"
        add_task_event(db, task, "cancel_failed", "远端任务进程回收失败", detail_json={"error": str(exc)})


def finish_task(db: Session, task: Task, state: str, message: str, return_code: int | None) -> None:
    """统一完成任务状态、结束时间、返回码和事件。"""
    task.state = state
    task.finished_at = local_datetime()
    task.return_code = return_code
    task.last_block_reason = ""
    add_task_event(db, task, state, message, detail_json={"return_code": return_code})
    append_task_log(task, f"\nProgram Exit With Code {return_code if return_code is not None else 'unknown'}\n")
    guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
    if guard is not None:
        guard.state = state


def load_open_allocation(db: Session, task_pk: int) -> TaskAllocation | None:
    """读取任务当前未释放的 allocation。"""
    return db.scalar(
        select(TaskAllocation)
        .where(TaskAllocation.task_id == task_pk)
        .where(TaskAllocation.released_at.is_(None))
        .order_by(TaskAllocation.id.desc())
    )


def allocation_cuda_indices(db: Session, allocation: TaskAllocation) -> list[int]:
    """把 allocation 中的 GPU ID 转为节点本地 CUDA index。"""
    if not allocation.gpu_ids:
        return []
    gpus = db.scalars(select(Gpu).where(Gpu.id.in_([coerce_int(item) for item in allocation.gpu_ids]))).all()
    return [gpu.gpu_index for gpu in sorted(gpus, key=lambda item: item.gpu_index)]


def ensure_guard(db: Session, task: Task, allocation: TaskAllocation) -> TaskRuntimeGuard:
    """确保运行时守护记录存在，executor 启动后补充 PID/PGID。"""
    guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
    if guard is None:
        guard = TaskRuntimeGuard(
            task_id=task.id,
            node_id=allocation.node_id,
            allocated_gpu_ids=[coerce_int(item) for item in allocation.gpu_ids],
            state="starting",
        )
        db.add(guard)
        db.flush()
    return guard


def miniconda_activate_path(settings: Settings) -> str:
    """根据 miniconda python 路径推导 activate 脚本路径，避免新增配置项。"""
    text = settings.miniconda_python
    if text.endswith("/bin/python"):
        return text[: -len("/bin/python")] + "/bin/activate"
    return str(Path(text).parent / "activate")


def runtime_metadata_path(settings: Settings, task_id: str) -> str:
    """远端 runner 写入 PID 元数据的位置。"""
    return f"{settings.runtime_root.rstrip('/')}/tasks/{task_id}.json"


def runtime_status_path(settings: Settings, task_id: str) -> str:
    """远端 wrapper 写入退出码的位置。"""
    return f"{settings.runtime_root.rstrip('/')}/tasks/{task_id}.status.json"


def coerce_int(value) -> int:
    """安全转换整数；异常值返回 0，避免 worker 被脏状态文件带崩。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def main() -> None:
    """任务执行器入口，负责 SSH 启动、停止和结果归档。"""
    run_forever("nebulagrid-executor", 1, task_executor_tick)


if __name__ == "__main__":
    main()
