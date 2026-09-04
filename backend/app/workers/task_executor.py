import json
import logging
import shlex
import subprocess
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.path_resolver import resolve_user_visible_path
from app.core.time_utils import ensure_local_datetime, local_datetime
from app.db.models import Env, Gpu, Node, Task, TaskAllocation, TaskEvent, TaskRuntimeGuard, User
from app.db.session import SessionLocal
from app.services.task_service import (
    ALLOC_ERROR_STOP_REASON_PREFIX,
    TERMINAL_STATES,
    add_task_event,
    append_task_log,
    release_task_allocations,
    write_task_cancel_marker,
)
from app.workers.common import run_forever

logger = logging.getLogger(__name__)
AUTO_STOP_REASON_PREFIX = "系统检测到远端执行异常"
STOPPING_BATCH_SIZE = 10
ACTIVE_BATCH_SIZE = 50
_stopping_task_cursor = 0
_active_task_cursor = 0


def task_executor_tick() -> None:
    """派发 dispatching 任务，并回收运行中任务的远端状态。"""
    global _active_task_cursor, _stopping_task_cursor
    settings = get_settings()
    with SessionLocal() as db:
        # 升级前的实现会先写 cancelled，再尝试远端 kill；若 kill 失败，任务已经误入历史区。
        # 对仍持有 allocation 的旧记录恢复两阶段状态，让它们重新进入可见且可重试的停止队列。
        restored_legacy = restore_unconfirmed_legacy_cancellations(db)
        if restored_legacy:
            db.commit()
        # 停止确认必须优先于新任务启动；否则单次启动可等待 120 秒，多条串行启动会放大停止延迟。
        # 用循环游标避免最早的一批不可达任务长期占满 LIMIT，导致后续停止请求永远得不到处理。
        stopping_tasks, _stopping_task_cursor = load_rotating_task_batch(
            db,
            {"cancelling"},
            _stopping_task_cursor,
            STOPPING_BATCH_SIZE,
        )
        for task in stopping_tasks:
            collect_remote_status(db, task, settings)
            db.commit()

        # 调度器每轮最多分配一条任务，executor 同样每轮只启动一条，避免慢节点长期阻塞回收循环。
        dispatching_task_ids = db.scalars(select(Task.id).where(Task.state == "dispatching").limit(1)).all()
        started = 0
        for task_id in dispatching_task_ids:
            task = claim_dispatching_task(db, task_id)
            if task is None:
                continue
            db.commit()
            started += 1
            start_remote_task(db, task, settings)
            db.commit()
        active_tasks, _active_task_cursor = load_rotating_task_batch(
            db,
            {"starting", "running"},
            _active_task_cursor,
            ACTIVE_BATCH_SIZE,
        )
        for task in active_tasks:
            collect_remote_status(db, task, settings)
            db.commit()
        logger.info(
            "executor handled stopping=%s dispatching=%s started=%s active=%s",
            len(stopping_tasks),
            len(dispatching_task_ids),
            started,
            len(active_tasks),
        )


def load_rotating_task_batch(
    db: Session,
    states: set[str],
    after_task_pk: int,
    limit: int,
) -> tuple[list[Task], int]:
    """从上次主键后继续取任务并在末尾回绕，保证固定 LIMIT 下所有任务最终都有机会被检查。"""
    items = db.scalars(
        select(Task)
        .where(Task.state.in_(states))
        .where(Task.id > after_task_pk)
        .order_by(Task.id)
        .limit(limit)
    ).all()
    if len(items) < limit and after_task_pk > 0:
        wrapped = db.scalars(
            select(Task)
            .where(Task.state.in_(states))
            .where(Task.id <= after_task_pk)
            .order_by(Task.id)
            .limit(limit - len(items))
        ).all()
        items.extend(wrapped)
    return items, (items[-1].id if items else 0)


def restore_unconfirmed_legacy_cancellations(db: Session) -> int:
    """恢复升级前未确认退出的取消/越权终态，避免遗留远端进程失联。"""
    task_ids = db.scalars(
        select(Task.id)
        .join(TaskAllocation, TaskAllocation.task_id == Task.id)
        .where(Task.state.in_({"cancelled", "alloc_error"}))
        .where(TaskAllocation.released_at.is_(None))
        .order_by(Task.id)
        .distinct()
        .limit(50)
    ).all()
    restored = 0
    for task_id in task_ids:
        task = db.scalar(
            select(Task)
            .where(Task.id == task_id)
            .where(Task.state.in_({"cancelled", "alloc_error"}))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            continue
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
        # 新流程会在同一个事务里写终态并释放全部 allocation，因此“终态 + 开放 allocation”
        # 本身就是旧版或异常记录。旧版 guard=cancelled 也没有强制存活核验，不能据此跳过恢复。
        previous_state = task.state
        task.state = "cancelling"
        task.finished_at = None
        if previous_state == "alloc_error":
            # 旧 Runtime Guard 会在 TERM 结果未知时直接写 alloc_error；保留前缀以便确认后回到正确终态。
            task.last_block_reason = (
                f"{ALLOC_ERROR_STOP_REASON_PREFIX}；检测到升级前越权终态尚未确认，正在重新核查远端进程"
            )
        else:
            task.last_block_reason = "检测到升级前停止状态尚未确认，正在重新核查远端进程"
        if guard is not None and guard.state != "cancel_failed":
            guard.state = "cancelling"
        add_task_event(
            db,
            task,
            "cancelling",
            "检测到未确认停止的历史任务，已恢复远端退出核查",
            detail_json={"previous_state": previous_state},
        )
        restored += 1
    return restored


def claim_dispatching_task(db: Session, task_pk: int) -> Task | None:
    """先用条件更新原子领取任务，避免用户中止 dispatching 时被执行器旧对象覆盖为 running。"""
    result = db.execute(
        update(Task)
        .where(Task.id == task_pk)
        .where(Task.state == "dispatching")
        .values(state="starting", last_block_reason="")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    task = db.get(Task, task_pk)
    if task is None:
        db.rollback()
        return None
    add_task_event(db, task, "starting", "任务执行器已领取，正在启动远端进程")
    return task


def start_remote_task(db: Session, task: Task, settings: Settings) -> None:
    """通过 SSH 调用远端 runner 启动任务，并记录 PID、日志和执行时间。"""
    db.refresh(task)
    if task.state == "cancelling":
        # API 已经写入停止意图，当前轮不再启动；后续由停止确认分支统一收敛状态。
        return
    if task.state != "starting":
        return
    open_allocations = load_open_allocations(db, task.id)
    if not open_allocations:
        task = lock_task_for_update(db, task.id)
        if task is None:
            return
        if task.state == "starting":
            finish_task(db, task, "failed", "任务缺少资源分配记录", return_code=None)
        elif task.state == "cancelling":
            # 本 executor 已领取任务但尚未调用远端 runner，可以确定本次 launch 没有进程。
            finalize_stopping_task(db, task, None, "执行器确认尚未调用远端 runner")
        return
    if len(open_allocations) > 1:
        task = lock_task_for_update(db, task.id)
        if task is not None and task.state not in TERMINAL_STATES:
            if task.state != "cancelling":
                task.last_block_reason = f"{AUTO_STOP_REASON_PREFIX}：检测到重叠 allocation"
            mark_untracked_stop_retry(
                db,
                task,
                f"检测到 {len(open_allocations)} 条未释放 allocation，无法安全确定需要停止的 launch",
            )
        return
    allocation = open_allocations[0]
    node = db.get(Node, allocation.node_id)
    owner = db.get(User, task.user_id)
    if node is None or owner is None:
        task = lock_task_for_update(db, task.id)
        if task is None:
            return
        if task.state == "starting":
            finish_task(db, task, "offline_error", "节点或用户记录不存在", return_code=None)
            release_task_allocations(task.id, db)
        elif task.state == "cancelling":
            # 缺失发生在 SSH 调用前，因此停止请求可以直接确认，不需要猜测远端状态。
            finalize_stopping_task(db, task, allocation, "执行器确认尚未调用远端 runner")
        return
    try:
        command = build_generated_command(task, db, settings)
        workdir = resolve_user_visible_path(task.workdir, owner.username, owner.role)
        cuda_indices = allocation_cuda_indices(db, allocation)
        runtime_path = runtime_metadata_path(settings, task.task_id)
        status_path = runtime_status_path(settings, task.task_id)
        cancel_path = runtime_cancel_path(settings, task.task_id)
    except Exception as exc:  # noqa: BLE001 - 尚未调用 SSH，此时可以确定远端进程没有被创建。
        locked_task = lock_task_for_update(db, task.id)
        if locked_task is not None and locked_task.state == "starting":
            logger.warning("failed to prepare task %s locally: %s", locked_task.task_id, exc)
            append_task_log(locked_task, f"\nNebulaGrid failed before remote launch: {exc}\n")
            finish_task(db, locked_task, "failed", "远端启动前准备失败", return_code=None)
            release_task_allocations(locked_task.id, db)
        elif locked_task is not None and locked_task.state == "cancelling":
            finalize_stopping_task(db, locked_task, allocation, "执行器确认本地准备失败前尚未调用远端 runner")
        return

    try:
        output = run_remote_runner(
            node,
            settings,
            task_id=task.task_id,
            launch_id=allocation.id,
            workdir=str(workdir).replace("\\", "/"),
            command=command,
            log_path=task.log_path,
            runtime_path=runtime_path,
            status_path=status_path,
            cancel_path=cancel_path,
            cuda_visible_devices=",".join(str(index) for index in cuda_indices),
        )
        metadata = parse_json_output(output)
        if not metadata:
            raise ValueError("remote runner did not return valid JSON metadata")
        if not control_payload_matches_launch(metadata, allocation.id):
            raise ValueError("remote runner returned metadata for a stale allocation")
    except Exception as exc:  # noqa: BLE001 - worker 必须把启动失败转为任务状态，而不是退出。
        task = lock_task_for_update(db, task.id)
        if task is None:
            return
        if task.state in TERMINAL_STATES:
            # 其他 executor 已完成同一任务时不再用本次慢 SSH 的旧结果覆盖终态。
            return
        status = load_runtime_status(locals().get("status_path"), allocation.id)
        if finish_from_confirmed_remote_failure(
            db,
            task,
            allocation,
            status,
            "远端 runner 已确认启动失败且未留下进程",
        ):
            return
        if finish_from_explicit_return_code(db, task, status):
            release_task_allocations(task.id, db)
            return
        # check_output 的异常仍可能携带 runner 写到 stdout 的完整回执；NFS 本地视图延迟时，
        # stdout 中的同 launch 返回码同样是明确结果，不能先进入停止流程。
        error_output = getattr(exc, "output", "") or ""
        candidate = parse_json_output(error_output)
        if not control_payload_matches_launch(candidate, allocation.id):
            candidate = {}
        if finish_from_confirmed_remote_failure(
            db,
            task,
            allocation,
            candidate,
            "远端 runner 已确认启动失败且未留下进程",
        ):
            return
        if finish_from_explicit_return_code(db, task, candidate):
            release_task_allocations(task.id, db)
            return
        if task.state != "cancelling":
            logger.warning("failed to start task %s: %s", task.task_id, exc)
            append_task_log(task, f"\nNebulaGrid failed to start task: {exc}\n")
            begin_start_failure_cleanup(db, task, allocation, str(exc))
        metadata = load_runtime_metadata(locals().get("runtime_path"), allocation.id)
        if not metadata and candidate:
            # runner 以非零码退出时 check_output 会抛异常，但 stdout 仍可能带有完整的 launch_failed 回执。
            metadata = candidate
        if remote_runner_exited_before_metadata(exc, metadata):
            # 远端 runner 已返回非 SSH 传输错误，且没有本次 launch 的控制元数据。
            # 该组合只会在 Popen 前失败（例如旧 runner 不认识新增参数）时出现，
            # 可以直接归档为失败；其余异常仍进入停止追踪，避免误放潜在远端进程。
            finish_remote_execution_failure(
                db,
                task,
                allocation,
                exc.returncode,
                f"远端 runner 启动命令退出，返回码 {exc.returncode}",
            )
            return
        update_guard_from_metadata(db, task, allocation, metadata)
        return

    # SSH 是外部慢操作，不能在其执行期间持数据库行锁；拿到回执后再按 task->guard
    # 顺序锁定并刷新状态，使并发停止或强制下线不会被旧 ORM 对象覆盖回 running。
    task = lock_task_for_update(db, task.id)
    if task is None:
        return
    if task.state in TERMINAL_STATES:
        # 并发收集者可能已根据同一 launch 的返回码完成任务，避免旧启动回执复活或改写终态。
        return
    status = load_runtime_status(status_path, allocation.id)
    if finish_from_confirmed_remote_failure(
        db,
        task,
        allocation,
        status,
        "远端 runner 已确认启动失败且未留下进程",
    ):
        return
    if finish_from_explicit_return_code(db, task, status):
        release_task_allocations(task.id, db)
        return
    if status.get("state") in {"launch_failed", "wrapper_failed"}:
        # runner/wrapper 基础设施失败没有用户返回码，必须先转入停止追踪；若回执同时证明
        # 根本未创建进程，才可以在同一事务内完成错误归档。
        begin_start_failure_cleanup(
            db,
            task,
            allocation,
            str(status.get("error") or "remote execution wrapper failed"),
        )
        if control_payload_confirms_stopped(status, allocation.id):
            finalize_stopping_task(db, task, allocation, "远端失败回执确认未留下进程")
        return
    if finish_from_confirmed_remote_failure(
        db,
        task,
        allocation,
        metadata,
        "远端 runner 已确认启动失败且未留下进程",
    ):
        return
    if finish_from_explicit_return_code(db, task, metadata):
        # runner stdout 与状态文件属于同一 launch；任一渠道已有整数返回码都应直接归档。
        release_task_allocations(task.id, db)
        return
    guard = update_guard_from_metadata(db, task, allocation, metadata)
    metadata_state = str(metadata.get("state") or "")
    if control_payload_confirms_stopped(metadata, allocation.id):
        if task.state != "cancelling":
            begin_start_failure_cleanup(db, task, allocation, "远端 runner 已确认启动阶段未留下进程")
        finalize_stopping_task(db, task, allocation, "远端 runner 已确认启动阶段未留下进程")
        return
    if metadata_state == "launch_failed":
        if task.state != "cancelling":
            begin_start_failure_cleanup(
                db,
                task,
                allocation,
                str(metadata.get("error") or "remote runner launch failed"),
            )
        return
    if task.state == "cancelling" or metadata_state == "cancelled":
        if task.state != "cancelling":
            task.state = "cancelling"
            task.finished_at = None
            task.last_block_reason = "停止标记已由远端 runner 确认，正在完成进程退出核查"
        guard.state = "cancelling"
        return
    if task.state != "starting":
        begin_start_failure_cleanup(db, task, allocation, f"unexpected task state after launch: {task.state}")
        return
    if metadata_state != "running":
        begin_start_failure_cleanup(db, task, allocation, f"unexpected runner state: {metadata_state or 'missing'}")
        return
    if not metadata_has_process_identity(metadata, allocation.id):
        begin_start_failure_cleanup(db, task, allocation, "remote runner metadata did not contain a safe process identity")
        return
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
    # 始终先锁 task 再读取或修改 guard，避免多个 executor、停止请求和节点管理操作互相覆盖。
    task = lock_task_for_update(db, task.id)
    if task is None or task.state in TERMINAL_STATES:
        return
    if task.state not in {"starting", "running", "cancelling"}:
        return
    if task.state == "cancelling" and finalize_cancelling_timeout(db, task, settings):
        return
    open_allocations = load_open_allocations(db, task.id)
    if len(open_allocations) > 1:
        # 控制文件目前按 task_id 命名，重叠 allocation 无法分别证明每个 launch 都已退出。
        # 保留全部占用并要求人工排查，绝不能只验证最新一条后释放所有记录。
        if task.state != "cancelling":
            task.last_block_reason = f"{AUTO_STOP_REASON_PREFIX}：检测到重叠 allocation"
        mark_untracked_stop_retry(
            db,
            task,
            f"检测到 {len(open_allocations)} 条未释放 allocation，无法安全确认全部远端进程退出",
        )
        return
    allocation = open_allocations[0] if open_allocations else None
    if allocation is None:
        if task.state == "cancelling":
            # 只有 wait/on_hold 会在 API 层无 allocation 直接结束；活跃任务缺 allocation 属于异常，
            # 不能据此推断远端从未启动，否则会重现“历史区已停止但节点仍运行”的问题。
            mark_untracked_stop_retry(db, task, "任务缺少未释放的 allocation，无法确认远端进程状态")
        return
    node = db.get(Node, allocation.node_id)
    if node is None:
        if task.state != "cancelling":
            begin_start_failure_cleanup(db, task, allocation, "节点记录不存在")
        mark_stop_retry(db, task, allocation, "节点记录不存在，无法确认远端进程退出")
        return
    if task.state == "cancelling":
        collect_stopping_status(db, task, allocation, node, settings)
        return
    try:
        status_text = read_remote_status(node, settings, runtime_status_path(settings, task.task_id))
    except Exception as exc:  # noqa: BLE001 - 在线节点短暂 SSH 抖动时保留运行状态。
        if node.state != "online":
            begin_start_failure_cleanup(db, task, allocation, f"运行中节点掉线: {exc}")
        else:
            logger.debug("task %s status not ready: %s", task.task_id, exc)
        return
    if not status_text.strip():
        return
    status = parse_json_output(status_text)
    if not status:
        logger.warning("task %s status file is not json: %s", task.task_id, status_text[:200])
        return
    if not control_payload_matches_launch(status, allocation.id):
        logger.warning("task %s ignored stale status for launch %s", task.task_id, status.get("launch_id"))
        return
    if finish_from_explicit_return_code(db, task, status):
        release_task_allocations(task.id, db)
        return
    if status.get("state") in {"launch_failed", "wrapper_failed"}:
        # 运行期 wrapper 自身失败不是用户程序返回码；先保留 allocation 转入停止追踪，
        # 再由下一轮使用 runtime 中的进程身份回收整个 launch。
        begin_start_failure_cleanup(
            db,
            task,
            allocation,
            str(status.get("error") or "remote execution wrapper failed"),
        )
        if control_payload_confirms_stopped(status, allocation.id):
            finalize_stopping_task(db, task, allocation, "远端失败回执确认未留下进程")


def collect_stopping_status(
    db: Session,
    task: Task,
    allocation: TaskAllocation,
    node: Node,
    settings: Settings,
) -> None:
    """处理停止中任务：先接收明确返回码，再补 PID、发送信号并确认进程已经不存在。"""
    write_task_cancel_marker(task.task_id, allocation.id)
    status_path = runtime_status_path(settings, task.task_id)
    runtime_path = runtime_metadata_path(settings, task.task_id)
    status = load_runtime_status(status_path, allocation.id)
    if finish_from_explicit_return_code(db, task, status):
        release_task_allocations(task.id, db)
        return
    if control_payload_confirms_stopped(status, allocation.id):
        finalize_stopping_task(db, task, allocation, "远端 runner 已确认进程停止")
        return

    metadata = load_runtime_metadata(runtime_path, allocation.id)
    update_guard_from_metadata(db, task, allocation, metadata)
    if control_payload_confirms_stopped(metadata, allocation.id):
        finalize_stopping_task(db, task, allocation, "远端运行记录已确认进程停止")
        return
    if metadata_has_process_identity(metadata, allocation.id):
        if stop_remote_process(db, task, node, settings, allocation, metadata):
            finish_after_confirmed_remote_stop(db, task, allocation, node, settings)
        return

    # 本地 NFS 视图可能暂时落后，再通过新的 SSH 会话读取节点侧控制文件。
    try:
        remote_status_text = read_remote_status(node, settings, status_path)
        remote_runtime_text = read_remote_status(node, settings, runtime_path)
    except Exception as exc:  # noqa: BLE001 - 连接不可用时必须保留 stopping 和 allocation，等待下一轮。
        mark_stop_retry(db, task, allocation, f"停止确认 SSH 失败: {exc}")
        return
    remote_status = parse_control_payload(remote_status_text, allocation.id)
    if finish_from_explicit_return_code(db, task, remote_status):
        release_task_allocations(task.id, db)
        return
    if control_payload_confirms_stopped(remote_status, allocation.id):
        finalize_stopping_task(db, task, allocation, "远端状态文件已确认进程停止")
        return
    remote_metadata = parse_control_payload(remote_runtime_text, allocation.id)
    update_guard_from_metadata(db, task, allocation, remote_metadata)
    if control_payload_confirms_stopped(remote_metadata, allocation.id):
        finalize_stopping_task(db, task, allocation, "远端运行记录已确认进程停止")
        return
    if metadata_has_process_identity(remote_metadata, allocation.id):
        if stop_remote_process(db, task, node, settings, allocation, remote_metadata):
            finish_after_confirmed_remote_stop(db, task, allocation, node, settings)
        return
    # runtime/status 都缺失不能证明进程从未启动：控制文件可能被清理，NFS 视图也可能落后。
    # 只有同 launch 的明确回执或经过身份校验的进程退出才能归档。
    mark_stop_retry(db, task, allocation, "尚未取得可验证的 PID 或停止回执")


def finalize_cancelling_timeout(db: Session, task: Task, settings: Settings) -> bool:
    """停止确认超时后强制归档为未知，并显式记录仍可能存在远端残留进程。"""
    stopping_event = db.scalar(
        select(TaskEvent)
        .where(TaskEvent.task_id == task.id)
        .where(TaskEvent.event_type.in_({"cancelling", "alloc_error_cancelling"}))
        .order_by(TaskEvent.id)
        .limit(1)
    )
    started_at = ensure_local_datetime(stopping_event.created_at if stopping_event is not None else None)
    if started_at is None:
        # 兼容旧记录或异常写入：没有停止事件时，最晚以任务启动/创建时间作为上限锚点，
        # 防止这类任务绕过超时回收而永久占用资源。
        started_at = ensure_local_datetime(task.started_at) or ensure_local_datetime(task.created_at)
    if started_at is None:
        return False
    elapsed_seconds = (local_datetime() - started_at).total_seconds()
    if elapsed_seconds < settings.cancelling_timeout_seconds:
        return False

    timeout_seconds = settings.cancelling_timeout_seconds
    task.state = "unknown"
    task.finished_at = local_datetime()
    task.return_code = None
    task.last_block_reason = (
        f"停止确认超过 {timeout_seconds} 秒仍未获得远端结果；状态未知，已释放调度资源，请人工核查节点残留进程"
    )[:512]
    guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
    if guard is not None:
        guard.state = "unknown"
    release_task_allocations(task.id, db)
    add_task_event(
        db,
        task,
        "unknown",
        "停止确认超时，远端最终状态未知，系统已强制归档并释放资源",
        detail_json={"timeout_seconds": timeout_seconds, "cancelling_since": started_at.isoformat()},
    )
    append_task_log(
        task,
        f"\nNebulaGrid stop confirmation timed out after {timeout_seconds} seconds; remote state is unknown. "
        "Resources were released and the node requires manual inspection.\n",
    )
    logger.warning(
        "task %s cancelling confirmation timed out after %s seconds; released allocations with unknown remote state",
        task.task_id,
        timeout_seconds,
    )
    return True


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
    task_id: str,
    launch_id: int,
    workdir: str,
    command: str,
    log_path: str,
    runtime_path: str,
    status_path: str,
    cancel_path: str,
    cuda_visible_devices: str,
) -> str:
    """在计算节点执行远端 runner.py，并返回其 JSON 元数据输出。"""
    remote_script = f"{settings.remote_code_root.rstrip('/')}/runner.py"
    remote_command = shlex.join(
        [
            settings.miniconda_python,
            remote_script,
            "--task-id",
            task_id,
            "--launch-id",
            str(launch_id),
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
            "--cancel-path",
            cancel_path,
            "--cuda-visible-devices",
            cuda_visible_devices,
        ]
    )
    ssh_command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={settings.ssh_connect_timeout_seconds}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{node.ssh_user}@{node.ip}",
        remote_command,
    ]
    return subprocess.check_output(
        ssh_command,
        text=True,
        stderr=subprocess.STDOUT,
        timeout=max(settings.task_start_timeout_seconds, settings.ssh_connect_timeout_seconds + 5),
    )


def read_remote_status(node: Node, settings: Settings, status_path: str) -> str:
    """读取远端状态文件；不存在时返回空文本。"""
    command = f"test -f {shlex.quote(status_path)} && cat {shlex.quote(status_path)} || true"
    ssh_command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={settings.ssh_connect_timeout_seconds}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{node.ssh_user}@{node.ip}",
        command,
    ]
    return subprocess.check_output(
        ssh_command,
        text=True,
        stderr=subprocess.STDOUT,
        timeout=max(settings.ssh_operation_timeout_seconds, settings.ssh_connect_timeout_seconds + 5),
    )


def parse_json_output(output: str) -> dict:
    """从后向前提取 JSON 对象，允许 SSH warning 或登录提示与 runner 回执同时出现。"""
    for line in reversed((output or "").splitlines()):
        try:
            payload = json.loads(line.strip())
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def parse_control_payload(output: str, launch_id: int) -> dict:
    """解析并校验控制文件所属 allocation，避免历史运行的状态文件污染本次任务。"""
    payload = parse_json_output(output)
    return payload if control_payload_matches_launch(payload, launch_id) else {}


def control_payload_matches_launch(payload: dict, launch_id: int) -> bool:
    """严格校验 launch_id；无代次的旧文件不能用于结束或中止当前 allocation。"""
    if not payload:
        return False
    payload_launch_id = payload.get("launch_id")
    if payload_launch_id is None:
        return False
    return strict_positive_int(payload_launch_id) == int(launch_id)


def load_control_file(path_value: str | None, launch_id: int) -> dict:
    """读取本地 NFS 控制文件；原子替换前的缺失或损坏内容留待下一轮重试。"""
    if not path_value:
        return {}
    try:
        output = Path(path_value).read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_control_payload(output, launch_id)


def load_runtime_metadata(runtime_path: str | None, launch_id: int) -> dict:
    """启动 SSH 结果不确定时，从共享 runtime 文件补读本次 allocation 的 PID。"""
    return load_control_file(runtime_path, launch_id)


def load_runtime_status(status_path: str | None, launch_id: int) -> dict:
    """读取本次 allocation 的远端结果；明确 return_code 优先于任何停止请求。"""
    return load_control_file(status_path, launch_id)


def explicit_return_code(payload: dict) -> int | None:
    """只接受实际存在的整数返回码，避免缺失或 null 被 coerce_int 错当成成功码 0。"""
    if "return_code" not in payload:
        return None
    value = payload.get("return_code")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned and cleaned.lstrip("+-").isdecimal():
            return int(cleaned)
    return None


def finish_from_explicit_return_code(db: Session, task: Task, payload: dict) -> bool:
    """任务已有明确返回码时直接结束；非零程序错误不再进入停止回收流程。"""
    return_code = explicit_return_code(payload)
    if return_code is None:
        return False
    if task.last_block_reason.startswith(ALLOC_ERROR_STOP_REASON_PREFIX):
        # GPU 越权属于平台策略错误；即使进程恰好自行退出，最终分类仍需保留 alloc_error。
        finish_task(db, task, "alloc_error", "Runtime Guard 检测到违规任务已经退出", return_code=return_code)
    else:
        final_state = "succeeded" if return_code == 0 else "failed"
        finish_task(db, task, final_state, "任务执行结束", return_code=return_code)
    return True


def finish_from_confirmed_remote_failure(
    db: Session,
    task: Task,
    allocation: TaskAllocation,
    payload: dict,
    message: str,
) -> bool:
    """仅在 runner 已确认无残留进程时，把远端基础设施失败直接归档为失败。"""
    if payload.get("state") not in {"launch_failed", "wrapper_failed"}:
        return False
    if not control_payload_confirms_stopped(payload, allocation.id):
        return False
    finish_remote_execution_failure(
        db,
        task,
        allocation,
        explicit_return_code(payload),
        str(payload.get("error") or message),
    )
    return True


def remote_runner_exited_before_metadata(exc: Exception, metadata: dict) -> bool:
    """识别 runner 在 Popen 前的确定失败，保留 SSH 断连和未知进程的停止追踪。"""
    if not isinstance(exc, subprocess.CalledProcessError):
        return False
    # 255 是 OpenSSH 的传输层失败码，无法证明节点侧没有继续运行的进程。
    # 正常 runner 只会在成功写出启动元数据后返回 0，因此 1..254 且无元数据
    # 表示远端 runner 自身在创建用户进程前已退出，例如参数不兼容或解释器异常。
    return 0 < exc.returncode < 255 and not metadata


def finish_remote_execution_failure(
    db: Session,
    task: Task,
    allocation: TaskAllocation,
    return_code: int | None,
    error: str,
) -> None:
    """记录已确认的远端执行异常，并在同一事务中释放对应分配。"""
    concise_error = str(error)[:320]
    logger.warning("remote runner failed for task %s: %s", task.task_id, concise_error)
    append_task_log(task, f"\nNebulaGrid remote execution failed: {concise_error}\n")
    finish_task(db, task, "failed", "远端执行异常", return_code=return_code)
    release_task_allocations(task.id, db)


def control_payload_confirms_stopped(payload: dict, launch_id: int) -> bool:
    """只有同一次 launch 的 runner 明确回执且确认进程已退出，才允许完成停止。"""
    if not control_payload_matches_launch(payload, launch_id):
        return False
    return payload.get("state") in {"cancelled", "launch_failed", "wrapper_failed"} and payload.get(
        "process_stopped"
    ) is True


def metadata_has_process_identity(payload: dict, launch_id: int) -> bool:
    """终止前必须具备 launch、PID、启动时钟和 boot ID，防止进程或节点重启后的身份复用。"""
    if not control_payload_matches_launch(payload, launch_id):
        return False
    boot_id = payload.get("boot_id")
    return (
        strict_positive_int(payload.get("pid")) > 0
        and strict_positive_int(payload.get("process_start_time")) > 0
        and isinstance(boot_id, str)
        and bool(boot_id.strip())
    )


def update_guard_from_metadata(
    db: Session,
    task: Task,
    allocation: TaskAllocation,
    metadata: dict,
) -> TaskRuntimeGuard:
    """把匹配本次 allocation 的 PID/PGID 补入 guard；缺失 PID 时仍保留停止中而不假定成功。"""
    guard = ensure_guard(db, task, allocation)
    if control_payload_matches_launch(metadata, allocation.id):
        root_pid = strict_positive_int(metadata.get("pid")) or None
        process_group_id = strict_positive_int(metadata.get("pgid")) or root_pid
        if root_pid is not None:
            guard.root_pid = root_pid
        if process_group_id is not None:
            guard.process_group_id = process_group_id
    if task.state == "cancelling" and guard.state != "cancel_failed":
        guard.state = "cancelling"
    db.flush()
    return guard


def begin_start_failure_cleanup(db: Session, task: Task, allocation: TaskAllocation, error: str) -> None:
    """把没有明确返回码的启动/连接异常转成停止中，先清理潜在远端进程再归档错误。"""
    concise_error = str(error)[:320]
    task.state = "cancelling"
    task.finished_at = None
    task.return_code = None
    task.last_block_reason = f"{AUTO_STOP_REASON_PREFIX}：{concise_error}；正在确认远端进程退出"[:512]
    guard = ensure_guard(db, task, allocation)
    guard.state = "cancelling"
    add_task_event(
        db,
        task,
        "cancelling",
        "远端启动或状态确认失败，任务进入停止中",
        # 停止标记由下一轮 executor 在本事务提交后补写，不能让文件意图领先于数据库状态。
        detail_json={"error": concise_error, "allocation_id": allocation.id, "cancel_marker_deferred": True},
    )


def mark_stop_retry(db: Session, task: Task, allocation: TaskAllocation, error: str) -> None:
    """停止尚未确认时保留任务和 allocation；同一连续失败只写一次事件，避免每秒刷屏。"""
    guard = ensure_guard(db, task, allocation)
    should_record = guard.state != "cancel_failed"
    guard.state = "cancel_failed"
    task.state = "cancelling"
    task.finished_at = None
    if should_record:
        add_task_event(
            db,
            task,
            "cancel_failed",
            "远端进程停止尚未确认，将在下一轮重试",
            detail_json={"error": error, "allocation_id": allocation.id},
        )


def mark_untracked_stop_retry(db: Session, task: Task, error: str) -> None:
    """活跃任务丢失 allocation 时保留停止中，并只记录一次需要人工核查的异常。"""
    if task.last_block_reason.startswith(ALLOC_ERROR_STOP_REASON_PREFIX):
        reason = f"{ALLOC_ERROR_STOP_REASON_PREFIX}；停止尚未确认：{error}"
    elif task.last_block_reason.startswith(AUTO_STOP_REASON_PREFIX):
        reason = f"{AUTO_STOP_REASON_PREFIX}；停止尚未确认：{error}"
    else:
        reason = f"停止尚未确认：{error}"
    should_record = task.last_block_reason != reason
    task.state = "cancelling"
    task.finished_at = None
    task.last_block_reason = reason
    guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
    if guard is not None:
        guard.state = "cancel_failed"
    if should_record:
        add_task_event(db, task, "cancel_failed", "缺少远端回收入口，任务继续保持停止中", detail_json={"error": error})


def finish_after_confirmed_remote_stop(
    db: Session,
    task: Task,
    allocation: TaskAllocation,
    node: Node,
    settings: Settings,
) -> None:
    """进程组为空后再从节点侧复读状态，封住自然退出返回码与停止确认之间的竞态。"""
    try:
        status_text = read_remote_status(node, settings, runtime_status_path(settings, task.task_id))
    except Exception as exc:  # noqa: BLE001 - 已确认进程退出，但返回码归类仍不能靠猜测。
        mark_stop_retry(db, task, allocation, f"进程已停止，但最终返回码复核失败: {exc}")
        return
    status = parse_control_payload(status_text, allocation.id)
    if finish_from_explicit_return_code(db, task, status):
        release_task_allocations(task.id, db)
        return
    finalize_stopping_task(db, task, allocation, "远端进程组已停止并通过存活检查")


def finalize_stopping_task(
    db: Session,
    task: Task,
    allocation: TaskAllocation | None,
    confirmation: str,
) -> None:
    """在收到停止回执或确认进程不存在后，才写终态、结束时间并释放调度资源。"""
    open_allocations = load_open_allocations(db, task.id)
    expected_ids = [] if allocation is None else [allocation.id]
    if [item.id for item in open_allocations] != expected_ids:
        # 避免异常的重叠 allocation 被“一次停止确认”整体释放；每个 launch 都必须能独立核查。
        mark_untracked_stop_retry(db, task, "allocation 集合在停止确认期间发生变化，拒绝归档")
        return
    automatic_cleanup = task.last_block_reason.startswith(AUTO_STOP_REASON_PREFIX)
    allocation_error_cleanup = task.last_block_reason.startswith(ALLOC_ERROR_STOP_REASON_PREFIX)
    task.finished_at = local_datetime()
    task.return_code = None
    guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
    if allocation_error_cleanup:
        task.state = "alloc_error"
        task.last_block_reason = f"{ALLOC_ERROR_STOP_REASON_PREFIX}，已确认远端进程停止"
        event_type = "alloc_error"
        message = "Runtime Guard 已确认违规任务进程停止"
        append_task_log(task, "\nNebulaGrid confirmed policy-violating process stopped\n")
    elif automatic_cleanup:
        task.state = "offline_error"
        task.last_block_reason = f"{AUTO_STOP_REASON_PREFIX}，已确认远端进程停止"
        event_type = "offline_error"
        message = "远端执行异常，潜在进程已确认停止后归档"
        append_task_log(task, "\nNebulaGrid confirmed remote process stopped after startup/status failure\n")
    else:
        task.state = "cancelled"
        task.last_block_reason = ""
        event_type = "cancelled"
        message = "远端进程已确认停止"
        append_task_log(task, "\nProgram Terminated By User\n")
    if guard is not None:
        guard.state = task.state
    release_task_allocations(task.id, db)
    add_task_event(
        db,
        task,
        event_type,
        message,
        detail_json={"confirmation": confirmation, "allocation_id": allocation.id if allocation else None},
    )


def stop_remote_process(
    db: Session,
    task: Task,
    node: Node,
    settings: Settings,
    allocation: TaskAllocation,
    metadata: dict,
) -> bool:
    """中止远端进程组并验证非僵尸进程已消失；任何不确定结果都保留 stopping 和 allocation。"""
    guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
    if guard is None or (guard.process_group_id is None and guard.root_pid is None):
        return False
    if not metadata_has_process_identity(metadata, allocation.id):
        mark_stop_retry(db, task, allocation, "缺少匹配本次启动的 PID 或进程启动时钟，拒绝执行不安全的终止")
        return False
    target = guard.process_group_id or guard.root_pid
    command = build_remote_termination_command(
        root_pid=guard.root_pid,
        process_group_id=guard.process_group_id,
        process_start_time=strict_positive_int(metadata.get("process_start_time")),
        boot_id=str(metadata.get("boot_id") or ""),
    )
    ssh_command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={settings.ssh_connect_timeout_seconds}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{node.ssh_user}@{node.ip}",
        command,
    ]
    try:
        subprocess.check_output(
            ssh_command,
            text=True,
            stderr=subprocess.STDOUT,
            timeout=max(settings.ssh_operation_timeout_seconds, settings.ssh_connect_timeout_seconds + 5),
        )
        guard.state = "cancelled"
        add_task_event(db, task, "stop_confirmed", "远端任务进程已停止并通过存活检查", detail_json={"target": target})
        return True
    except Exception as exc:  # noqa: BLE001 - 中止失败要保留占用，下一轮继续尝试回收。
        should_record = guard.state != "cancel_failed"
        guard.state = "cancel_failed"
        if should_record:
            add_task_event(db, task, "cancel_failed", "远端任务进程回收失败，将在下一轮重试", detail_json={"error": str(exc)})
        return False


def build_remote_termination_command(
    root_pid: int | None,
    process_group_id: int | None,
    process_start_time: int | None,
    boot_id: str,
) -> str:
    """校验 boot/PID/starttime/PGID 后只终止本次 launch 的进程组，并确认其中没有活动成员。"""
    root = int(root_pid or 0)
    pgid = int(process_group_id or root)
    expected_start = int(process_start_time or 0)
    expected_boot = shlex.quote(str(boot_id or ""))
    return f"""
root={root}
pgid={pgid}
expected_start={expected_start}
expected_boot={expected_boot}
probe_group() {{
  snapshot="$(ps -eo pgid=,stat= 2>/dev/null)" || {{ printf '%s\\n' unknown; return; }}
  printf '%s\\n' "$snapshot" | awk -v target="$pgid" '$1 == target && $2 !~ /^Z/ {{ found=1 }} END {{ exit(found ? 0 : 1) }}'
  probe_code=$?
  if [ "$probe_code" -eq 0 ]; then
    printf '%s\\n' live
  elif [ "$probe_code" -eq 1 ]; then
    printf '%s\\n' empty
  else
    printf '%s\\n' unknown
  fi
}}
if [ "$root" -le 0 ] || [ "$pgid" -le 0 ] || [ "$expected_start" -le 0 ] || [ -z "$expected_boot" ]; then
  echo "NebulaGrid stop verification failed: missing process identity" >&2
  exit 76
fi
current_boot="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)" || {{
  echo "NebulaGrid stop verification failed: cannot read node boot id" >&2
  exit 77
}}
if [ "$current_boot" != "$expected_boot" ]; then
  echo "NebulaGrid stop verification succeeded: original launch ended with previous node boot"
  exit 0
fi
if [ ! -r "/proc/$root/stat" ]; then
  group_state="$(probe_group)"
  if [ "$group_state" = unknown ]; then
    echo "NebulaGrid stop verification failed: process probe failed" >&2
    exit 77
  fi
  if [ "$group_state" = live ]; then
    echo "NebulaGrid stop verification failed: root PID disappeared while process group is still alive" >&2
    exit 76
  fi
  echo "NebulaGrid stop verification succeeded: process already absent"
  exit 0
fi
stat_line="$(cat "/proc/$root/stat" 2>/dev/null)" || {{
  echo "NebulaGrid stop verification failed: cannot read process identity" >&2
  exit 77
}}
# comm 字段可以包含空格和右括号；最长前缀删除可稳定取得最后一个 ') ' 后的字段。
stat_fields="${{stat_line##*) }}"
set -- $stat_fields
actual_pgid="$3"
actual_session="$4"
actual_start="${{20}}"
if [ "$actual_start" != "$expected_start" ] || [ "$actual_pgid" != "$pgid" ] || [ "$actual_session" != "$root" ]; then
  echo "NebulaGrid stop verification failed: process identity changed" >&2
  exit 76
fi
kill -TERM -"$pgid" 2>/dev/null || true
sleep 1
group_state="$(probe_group)"
if [ "$group_state" = unknown ]; then
  echo "NebulaGrid stop verification failed: process probe failed" >&2
  exit 77
fi
if [ "$group_state" = live ]; then
  kill -KILL -"$pgid" 2>/dev/null || true
fi
sleep 1
group_state="$(probe_group)"
if [ "$group_state" = unknown ]; then
  echo "NebulaGrid stop verification failed: process probe failed" >&2
  exit 77
fi
if [ "$group_state" = live ]; then
  echo "NebulaGrid stop verification failed: process group still alive" >&2
  exit 75
fi
echo "NebulaGrid stop verification succeeded"
"""


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


def load_open_allocations(db: Session, task_pk: int) -> list[TaskAllocation]:
    """最多读取两条开放 allocation；第二条用于阻止重叠 launch 被错误地整体释放。"""
    return list(
        db.scalars(
            select(TaskAllocation)
            .where(TaskAllocation.task_id == task_pk)
            .where(TaskAllocation.released_at.is_(None))
            .order_by(TaskAllocation.id.desc())
            .limit(2)
        ).all()
    )


def lock_task_for_update(db: Session, task_pk: int) -> Task | None:
    """按统一 task->guard 顺序锁定并刷新任务，防止并发停止、下线或多 executor 丢失状态。"""
    return db.scalar(
        select(Task)
        .where(Task.id == task_pk)
        .with_for_update()
        .execution_options(populate_existing=True)
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


def runtime_cancel_path(settings: Settings, task_id: str) -> str:
    """返回 allocation 感知的停止标记路径，runner 会在 Popen 前后检查该文件。"""
    return f"{settings.runtime_root.rstrip('/')}/tasks/{task_id}.cancel.json"


def coerce_int(value) -> int:
    """安全转换整数；异常值返回 0，避免 worker 被脏状态文件带崩。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def strict_positive_int(value) -> int:
    """控制协议只接受正整数或纯数字字符串，拒绝布尔值、浮点数及带小数的脏字段。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str) and value.strip().isdecimal():
        converted = int(value.strip())
        return converted if converted > 0 else 0
    return 0


def main() -> None:
    """任务执行器入口，负责 SSH 启动、停止和结果归档。"""
    run_forever("nebulagrid-executor", 1, task_executor_tick)


if __name__ == "__main__":
    main()
