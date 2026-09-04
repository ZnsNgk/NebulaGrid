from datetime import timedelta
import json
from pathlib import Path
import stat
import subprocess
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.time_utils import local_datetime
from app.services.dashboard_service import count_available_gpus
from app.services.auth_service import hash_session_token
from app.services.gpu_compatibility_service import pytorch_gpu_compatibility
from app.services.metrics_service import LatestMetrics, parse_flux_csv
from app.db.models import Env, Gpu, LoginSession, Node, Setting, Task, TaskAllocation, TaskEvent, TaskRuntimeGuard, User
from app.db.session import SessionLocal
from app.main import create_app
from app.remote import runner as remote_runner
from app.workers.node_monitor import (
    MonitorWatchdogTimeout,
    NodeMonitorTarget,
    NodeMonitorWorker,
    build_remote_monitor_command,
    monitor_reconnect_attempts,
    monitor_watchdog_timeout_seconds,
    monitor_node,
    node_monitor_paused,
    normalize_reconnect_attempts,
    normalize_watchdog_timeout,
    sync_gpu_inventory,
)
from app.workers.runtime_guard import expand_pid_tree, parse_gpu_apps, parse_process_table
from app.workers.scheduler import release_terminal_allocations, scheduler_interval_seconds, scheduler_tick, select_gpu_allocation
from app.workers import runtime_guard, task_executor


def make_client() -> TestClient:
    """创建隔离的测试客户端，避免测试直接复用全局 app 状态。"""
    return TestClient(create_app())


def create_remote_task_fixture(
    client: TestClient,
    headers: dict[str, str],
    tmp_path: Path,
    *,
    state: str = "running",
    root_pid: int | None = 4321,
) -> tuple[str, int]:
    """创建带开放 allocation 和 guard 的任务，供停止状态机测试复用且不访问真实节点。"""
    suffix = uuid4().hex[:8]
    node_response = client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": f"node-task-stop-{suffix}", "ip": "10.254.0.10", "gpu_schedulable_flags": [1]},
    )
    assert node_response.status_code == 200
    node_id = node_response.json()["data"]["id"]
    task_response = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "description": f"task-stop-{suffix}",
            "workdir": "/",
            "command": "python train.py",
            "requirement": {"need_gpus": 0, "node_id": node_id},
        },
    )
    assert task_response.status_code == 200
    task_id = task_response.json()["data"]["task_id"]

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task.state = state
        task.started_at = local_datetime() if state == "running" else None
        task.log_path = str(tmp_path / "logs" / f"{task_id}.log")
        allocation = TaskAllocation(
            task_id=task.id,
            node_id=node_id,
            gpu_ids=[],
            cpu_allocated=1,
            allocation_mode="cpu",
        )
        db.add(allocation)
        db.flush()
        db.add(
            TaskRuntimeGuard(
                task_id=task.id,
                node_id=node_id,
                root_pid=root_pid,
                process_group_id=root_pid,
                allocated_gpu_ids=[],
                state=state,
            )
        )
        db.commit()
        return task_id, allocation.id


def isolated_executor_settings(tmp_path: Path) -> Settings:
    """让 executor 控制文件和日志落在测试临时目录，避免依赖部署机的 NFS 路径。"""
    return Settings(
        runtime_root=str(tmp_path / "runtime"),
        task_log_root=str(tmp_path / "logs"),
    )


def write_runtime_identity(
    settings: Settings,
    task_id: str,
    allocation_id: int,
    *,
    launch_id: int | None = None,
    pid: int = 4321,
    pgid: int = 4321,
    process_start_time: int = 987654,
    boot_id: str = "test-node-boot-id",
) -> Path:
    """写入可校验的进程身份；测试可覆盖匹配或旧 launch 元数据两种边界。"""
    runtime_path = Path(task_executor.runtime_metadata_path(settings, task_id))
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "launch_id": allocation_id if launch_id is None else launch_id,
                "state": "running",
                "pid": pid,
                "pgid": pgid,
                "process_start_time": process_start_time,
                "boot_id": boot_id,
            }
        ),
        encoding="utf-8",
    )
    return runtime_path


def test_health_check_returns_ok() -> None:
    """验证健康检查接口使用统一响应格式并返回 ok 状态。"""
    client = make_client()
    response = client.get("/api/health")
    payload = response.json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["data"]["status"] == "ok"


def test_runtime_config_returns_shared_folder_root(monkeypatch, tmp_path: Path) -> None:
    """验证普通登录用户能读取前端展示所需的真实共享文件夹根目录。"""
    shared_root = tmp_path / "actual-shared"
    monkeypatch.setenv("NEBULAGRID_SHARED_FOLDER_ROOT", str(shared_root))
    get_settings.cache_clear()
    try:
        client = make_client()
        login_response = client.post("/api/auth/login", json={"identity": "admin", "password": "admin123"})
        token = login_response.json()["data"]["access_token"]

        response = client.get("/api/runtime-config", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        assert response.json()["data"]["shared_folder_root"] == str(shared_root)
    finally:
        get_settings.cache_clear()


def test_runtime_guard_matches_gpu_usage_from_pid_tree() -> None:
    """验证 Runtime Guard 会把子进程 GPU 使用计入任务 PID 树，避免只检查根 PID 漏报。"""
    process_table = parse_process_table(
        """
        100 1
        101 100
        102 101
        200 1
        """
    )
    task_pids = expand_pid_tree(100, process_table)
    observed = parse_gpu_apps(
        """
        102, GPU-task, 2048
        200, GPU-other, 1024
        """,
        task_pids,
    )

    assert task_pids == {100, 101, 102}
    assert observed == {"GPU-task"}


def test_parse_flux_csv_resets_headers_between_metric_tables() -> None:
    """验证 InfluxDB 多表头 CSV 会逐段解析，避免 GPU 表头把节点监控字段错位。"""
    content = """#group,false,false,true,true,true,true,true,true,true,true,true,true
,result,table,_start,_stop,_time,_value,_field,_measurement,gpu_id,gpu_index,gpu_uuid,model,node_id,node_name
,,0,2026-05-24T00:00:00Z,2026-05-24T01:00:00Z,2026-05-24T00:59:00Z,0,gpu_usage,gpu_metrics,12,0,GPU-1,RTX 4080,7,node-a
#group,false,false,true,true,true,true,true,true,true,true,true
,result,table,_start,_stop,_time,_value,_field,_measurement,gpu_id,ip,node_id,node_name
,,1,2026-05-24T00:00:00Z,2026-05-24T01:00:00Z,2026-05-24T00:59:00Z,62807,avail_ram_mb,node_metrics,none,10.0.0.7,7,node-a
"""
    rows = parse_flux_csv(content)

    assert rows[0]["_measurement"] == "gpu_metrics"
    assert rows[0]["gpu_id"] == "12"
    assert rows[0]["node_id"] == "7"
    assert rows[1]["_measurement"] == "node_metrics"
    assert rows[1]["gpu_id"] == "none"
    assert rows[1]["ip"] == "10.0.0.7"
    assert rows[1]["node_id"] == "7"


def test_dashboard_available_gpu_counts_only_schedulable_free_low_load_cards() -> None:
    """验证首页可用 GPU 是当前可调度资源，不把总数、已调度占用或外部高负载 GPU 计入。"""
    node = SimpleNamespace(
        state="online",
        scheduling_enabled=True,
        gpus=[
            SimpleNamespace(schedulable=True, scheduled_occupied=False, gpu_usage=5, free_vram_mb=23000, total_vram_mb=24576),
            SimpleNamespace(schedulable=True, scheduled_occupied=True, gpu_usage=0, free_vram_mb=24576, total_vram_mb=24576),
            SimpleNamespace(schedulable=True, scheduled_occupied=False, gpu_usage=90, free_vram_mb=24000, total_vram_mb=24576),
            SimpleNamespace(schedulable=False, scheduled_occupied=False, gpu_usage=0, free_vram_mb=24576, total_vram_mb=24576),
        ],
    )

    assert count_available_gpus([node]) == 1


def test_auth_login_and_me() -> None:
    """验证演示账号可以登录，并能通过 Bearer 令牌读取当前用户。"""
    client = make_client()
    login_response = client.post(
        "/api/auth/login",
        json={"identity": "admin", "password": "admin123"},
    )
    login_payload = login_response.json()
    token = login_payload["data"]["access_token"]

    me_response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    me_payload = me_response.json()

    assert login_response.status_code == 200
    assert me_response.status_code == 200
    assert me_payload["data"]["username"] == "admin"


def test_viewer_uses_presenter_endpoint_without_other_permissions() -> None:
    """验证展示者账号只能访问大屏聚合接口，且静默很久后会话仍保持有效。"""
    client = make_client()
    admin_token = login_as_admin(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "screen-viewer",
            "real_name": "Screen Viewer",
            "role": "viewer",
            "state": "enabled",
            "password": "viewer123",
        },
    )
    login_response = client.post("/api/auth/login", json={"identity": "screen-viewer", "password": "viewer123"})
    token = login_response.json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 展示者用于公共屏幕，不能因为超过普通 30 分钟在线窗口而被动下线。
    with SessionLocal() as db:
        session = db.scalar(select(LoginSession).where(LoginSession.token_hash == hash_session_token(token)))
        assert session is not None
        session.last_seen_at = local_datetime() - timedelta(days=365)
        db.commit()

    me_response = client.get("/api/auth/me", headers=headers)
    presenter_response = client.get("/api/dashboard/presenter", headers=headers)
    nodes_response = client.get("/api/nodes", headers=headers)
    tasks_response = client.get("/api/tasks", headers=headers)

    assert me_response.status_code == 200
    assert me_response.json()["data"]["permissions"] == ["presenter:read"]
    assert presenter_response.status_code == 200
    assert set(presenter_response.json()["data"]) == {"summary", "nodes"}
    assert nodes_response.status_code == 403
    assert tasks_response.status_code == 403


def test_task_lifecycle_smoke() -> None:
    """验证任务提交、查询、取消和日志占位接口可以形成最小闭环。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}

    create_response = client.post(
        "/api/tasks",
        headers=headers,
        json={"command": "python train.py", "workdir": "/"},
    )
    task_id = create_response.json()["data"]["task_id"]
    detail_response = client.get(f"/api/tasks/{task_id}", headers=headers)
    cancel_response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
    log_response = client.get(f"/api/tasks/{task_id}/log", headers=headers)

    assert create_response.status_code == 200
    assert detail_response.json()["data"]["task_id"] == task_id
    assert cancel_response.json()["data"]["state"] == "cancelled"
    assert task_id in log_response.text


def test_running_task_cancel_is_idempotent_and_keeps_allocation(monkeypatch, tmp_path: Path) -> None:
    """运行任务重复停止时保持 stopping 语义，确认前不能填写结束时间或释放 allocation。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path)
    marker_calls: list[tuple[str, int | None]] = []

    def fake_write_cancel_marker(target_task_id: str, launch_id: int | None) -> bool:
        """标记写入发生时，另一会话必须已经能看到已提交的 cancelling 状态。"""
        with SessionLocal() as verify_db:
            persisted = verify_db.scalar(select(Task).where(Task.task_id == target_task_id))
            assert persisted is not None
            assert persisted.state == "cancelling"
        marker_calls.append((target_task_id, launch_id))
        return True

    monkeypatch.setattr("app.services.task_service.write_task_cancel_marker", fake_write_cancel_marker)

    first_response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
    running_items = client.get(
        f"/api/tasks?state=running&search={task_id}&page_size=20",
        headers=headers,
    ).json()["data"]["items"]
    history_items = client.get(
        f"/api/tasks?state=history&search={task_id}&page_size=20",
        headers=headers,
    ).json()["data"]["items"]

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id)) if task else None
        event_types_before_retry = db.scalars(
            select(TaskEvent.event_type).where(TaskEvent.task_id == task.id)
        ).all() if task else []

    second_response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)

    with SessionLocal() as db:
        task_after_retry = db.scalar(select(Task).where(Task.task_id == task_id))
        event_types_after_retry = db.scalars(
            select(TaskEvent.event_type).where(TaskEvent.task_id == task_after_retry.id)
        ).all() if task_after_retry else []

    assert first_response.status_code == 200
    assert first_response.json()["data"]["state"] == "cancelling"
    assert first_response.json()["data"]["finished_at"] is None
    assert second_response.status_code == 200
    assert second_response.json()["data"]["state"] == "cancelling"
    assert task is not None
    assert task.state == "cancelling"
    assert task.finished_at is None
    assert allocation is not None
    assert allocation.released_at is None
    assert guard is not None
    assert guard.state == "cancelling"

    assert [item["task_id"] for item in running_items] == [task_id]
    assert history_items == []
    assert event_types_before_retry.count("cancelling") == 1
    assert event_types_after_retry.count("cancelling") == 1
    assert marker_calls == [(task_id, allocation_id)]


def test_overlapping_allocations_never_release_after_single_launch_confirmation(monkeypatch, tmp_path: Path) -> None:
    """异常重叠 launch 无法由单一 task 控制文件逐一证明退出，必须保留全部占用等待人工核查。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, first_allocation_id = create_remote_task_fixture(client, headers, tmp_path)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        first_allocation = db.get(TaskAllocation, first_allocation_id)
        assert task is not None and first_allocation is not None
        second_allocation = TaskAllocation(
            task_id=task.id,
            node_id=first_allocation.node_id,
            gpu_ids=[],
            cpu_allocated=1,
            allocation_mode="cpu",
        )
        db.add(second_allocation)
        db.commit()
        second_allocation_id = second_allocation.id

    monkeypatch.setattr("app.services.task_service.write_task_cancel_marker", lambda *_: True)
    response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "cancelling"

    monkeypatch.setattr(
        task_executor.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("overlapping launches must not be killed")),
    )
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, isolated_executor_settings(tmp_path))
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocations = db.scalars(
            select(TaskAllocation)
            .where(TaskAllocation.id.in_((first_allocation_id, second_allocation_id)))
            .order_by(TaskAllocation.id)
        ).all()
    assert task is not None and task.state == "cancelling"
    assert "重叠 allocation" in task.last_block_reason
    assert len(allocations) == 2
    assert all(allocation.released_at is None for allocation in allocations)


def test_explicit_return_code_finishes_cancelling_task_without_stop(monkeypatch, tmp_path: Path) -> None:
    """远端已有明确非零返回码时直接按失败归档，不再发送可能误杀新进程的停止命令。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path)
    monkeypatch.setattr("app.services.task_service.write_task_cancel_marker", lambda *_: True)
    cancel_response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
    assert cancel_response.json()["data"]["state"] == "cancelling"

    settings = isolated_executor_settings(tmp_path)
    status_path = Path(task_executor.runtime_status_path(settings, task_id))
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps({"launch_id": allocation_id, "state": "finished", "return_code": 17}),
        encoding="utf-8",
    )
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)

    def unexpected_stop(*_args, **_kwargs):
        """明确返回码必须优先完成任务，若进入终止分支则测试直接失败。"""
        raise AssertionError("stop_remote_process must not run after an explicit return code")

    monkeypatch.setattr(task_executor, "stop_remote_process", unexpected_stop)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id)) if task else None

    assert task is not None
    assert task.state == "failed"
    assert task.return_code == 17
    assert task.finished_at is not None
    assert allocation is not None
    assert allocation.released_at is not None
    assert guard is not None
    assert guard.state == "failed"


@pytest.mark.parametrize(("return_code", "expected_state"), [(0, "succeeded"), (17, "failed")])
def test_return_code_written_during_stop_probe_wins_over_cancel(
    return_code: int,
    expected_state: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """进程组核查为空后必须复读状态，保留恰在停止窗口自然结束的真实返回码。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path)
    monkeypatch.setattr("app.services.task_service.write_task_cancel_marker", lambda *_: True)
    assert client.post(f"/api/tasks/{task_id}/cancel", headers=headers).status_code == 200

    settings = isolated_executor_settings(tmp_path)
    write_runtime_identity(settings, task_id, allocation_id)
    final_status = json.dumps(
        {
            "task_id": task_id,
            "launch_id": allocation_id,
            "state": "finished",
            "return_code": return_code,
        }
    )
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)
    monkeypatch.setattr(task_executor, "stop_remote_process", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(task_executor, "read_remote_status", lambda *_: final_status)

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
    assert task is not None and task.state == expected_state
    assert task.return_code == return_code
    assert allocation is not None and allocation.released_at is not None


def test_start_uses_explicit_return_code_from_runner_stdout_without_cleanup(monkeypatch, tmp_path: Path) -> None:
    """NFS 状态视图尚未刷新时，runner stdout 中同 launch 的整数返回码也必须直接归档。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(
        client,
        headers,
        tmp_path,
        state="starting",
        root_pid=None,
    )
    settings = isolated_executor_settings(tmp_path)
    monkeypatch.setattr(task_executor, "resolve_user_visible_path", lambda *_: tmp_path)
    monkeypatch.setattr(
        task_executor,
        "run_remote_runner",
        lambda *_args, **_kwargs: json.dumps(
            {
                "task_id": task_id,
                "launch_id": allocation_id,
                "state": "finished",
                "return_code": 23,
            }
        ),
    )

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.start_remote_task(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
    assert task is not None and task.state == "failed"
    assert task.return_code == 23
    assert allocation is not None and allocation.released_at is not None


def test_runner_argument_error_finishes_failed_without_cancelling(monkeypatch, tmp_path: Path) -> None:
    """旧 runner 不认识新增参数时会在 Popen 前退出，任务应直接归档为失败。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(
        client,
        headers,
        tmp_path,
        state="starting",
        root_pid=None,
    )
    settings = isolated_executor_settings(tmp_path)
    monkeypatch.setattr(task_executor, "resolve_user_visible_path", lambda *_: tmp_path)

    def raise_runner_argument_error(*_args, **_kwargs):
        """模拟远端 argparse 直接返回 2；此时 runner 尚未创建用户进程。"""
        raise subprocess.CalledProcessError(
            returncode=2,
            cmd="ssh",
            output="usage: runner.py [-h]\\nrunner.py: error: unrecognized arguments: --cancel-path /tmp/cancel.json",
        )

    monkeypatch.setattr(task_executor, "run_remote_runner", raise_runner_argument_error)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.start_remote_task(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id)) if task else None

    assert task is not None
    assert task.state == "failed"
    assert task.return_code == 2
    assert task.finished_at is not None
    assert allocation is not None and allocation.released_at is not None
    assert guard is not None and guard.state == "failed"


def test_cancelling_timeout_forces_unknown_history_and_releases_allocations(monkeypatch, tmp_path: Path) -> None:
    """停止回执超时后必须释放占用并显式标记 unknown，而不是误报已停止。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path, state="cancelling")
    settings = Settings(
        runtime_root=str(tmp_path / "runtime"),
        task_log_root=str(tmp_path / "logs"),
        cancelling_timeout_seconds=30,
    )
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        db.add(
            TaskEvent(
                task_id=task.id,
                event_type="cancelling",
                message="测试停止中超时锚点",
                created_at=local_datetime() - timedelta(seconds=31),
            )
        )
        db.commit()

    monkeypatch.setattr(
        task_executor.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("超时归档不应再发起 SSH")),
    )
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id)) if task else None
        assert task is not None
        event_types = db.scalars(select(TaskEvent.event_type).where(TaskEvent.task_id == task.id)).all()

    assert task.state == "unknown"
    assert task.finished_at is not None
    assert task.return_code is None
    assert "30 秒" in task.last_block_reason
    assert allocation is not None and allocation.released_at is not None
    assert guard is not None and guard.state == "unknown"
    assert "unknown" in event_types


def test_start_timeout_stays_cancelling_until_runner_confirms_failure(monkeypatch, tmp_path: Path) -> None:
    """SSH 启动超时只表示结果未知，必须先进入停止追踪并继续保留调度资源。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(
        client,
        headers,
        tmp_path,
        state="starting",
        root_pid=None,
    )
    settings = isolated_executor_settings(tmp_path)
    monkeypatch.setattr(task_executor, "resolve_user_visible_path", lambda *_: tmp_path)
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)

    def raise_start_timeout(*_args, **_kwargs):
        """模拟 SSH 等待 runner 启动回执超时，而不是远端明确返回失败。"""
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=settings.task_start_timeout_seconds)

    monkeypatch.setattr(task_executor, "run_remote_runner", raise_start_timeout)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.start_remote_task(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id)) if task else None

    assert task is not None
    assert task.state == "cancelling"
    assert task.finished_at is None
    assert task.return_code is None
    assert allocation is not None
    assert allocation.released_at is None
    assert guard is not None
    assert guard.state == "cancelling"

    status_path = Path(task_executor.runtime_status_path(settings, task_id))
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "launch_id": allocation_id,
                "state": "launch_failed",
                "return_code": None,
                "process_stopped": True,
            }
        ),
        encoding="utf-8",
    )
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
    assert task is not None
    assert task.state == "failed"
    assert task.finished_at is not None
    assert allocation is not None
    assert allocation.released_at is not None


def test_start_timeout_recovers_late_runtime_and_stops_process_group(monkeypatch, tmp_path: Path) -> None:
    """复现低带宽事故：启动 SSH 超时后 PID 元数据迟到，executor 仍须回收原进程组再归档。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(
        client,
        headers,
        tmp_path,
        state="starting",
        root_pid=None,
    )
    settings = isolated_executor_settings(tmp_path)
    monkeypatch.setattr(task_executor, "resolve_user_visible_path", lambda *_: tmp_path)
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)

    def raise_start_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=settings.task_start_timeout_seconds)

    monkeypatch.setattr(task_executor, "run_remote_runner", raise_start_timeout)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.start_remote_task(db, task, settings)
        db.commit()

    # SSH 客户端虽然超时，远端 runner 仍可能稍后完成 Popen 并写出本次 launch 的身份。
    write_runtime_identity(settings, task_id, allocation_id)
    ssh_commands: list[list[str]] = []

    def confirm_stop(command, text=True, stderr=None, timeout=None):
        ssh_commands.append(list(command))
        if "probe_group()" in command[-1]:
            return "NebulaGrid stop verification succeeded\n"
        return ""

    monkeypatch.setattr(task_executor.subprocess, "check_output", confirm_stop)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)

    termination_commands = [command[-1] for command in ssh_commands if "probe_group()" in command[-1]]
    assert len(termination_commands) == 1
    assert "expected_boot=test-node-boot-id" in termination_commands[0]
    assert 'kill -TERM -"$pgid"' in termination_commands[0]
    assert task is not None and task.state == "offline_error"
    assert task.finished_at is not None
    assert allocation is not None and allocation.released_at is not None


def test_remote_runner_uses_configured_connection_and_start_timeouts(monkeypatch, tmp_path: Path) -> None:
    """启动命令分别使用可配置的 SSH 建连超时和更长的 runner 回执等待时间。"""
    settings = Settings(
        runtime_root=str(tmp_path / "runtime"),
        ssh_connect_timeout_seconds=23,
        task_start_timeout_seconds=137,
        ssh_operation_timeout_seconds=41,
    )
    observed: dict[str, object] = {}

    def fake_check_output(command, text=True, stderr=None, timeout=None):
        """捕获 subprocess 边界参数，不建立真实 SSH 连接。"""
        observed["command"] = command
        observed["timeout"] = timeout
        return json.dumps(
            {
                "task_id": "TASK-TIMEOUT",
                "launch_id": 19,
                "state": "running",
                "pid": 4321,
                "pgid": 4321,
                "process_start_time": 987654,
                "boot_id": "test-node-boot-id",
            }
        )

    monkeypatch.setattr(task_executor.subprocess, "check_output", fake_check_output)
    output = task_executor.run_remote_runner(
        SimpleNamespace(ssh_user="tester", ip="10.254.0.11"),
        settings,
        task_id="TASK-TIMEOUT",
        launch_id=19,
        workdir="/tmp",
        command="python train.py",
        log_path=str(tmp_path / "task.log"),
        runtime_path=str(tmp_path / "runtime.json"),
        status_path=str(tmp_path / "status.json"),
        cancel_path=str(tmp_path / "cancel.json"),
        cuda_visible_devices="0",
    )

    assert json.loads(output)["launch_id"] == 19
    assert "ConnectTimeout=23" in observed["command"]
    assert observed["timeout"] == 137

    task_executor.read_remote_status(
        SimpleNamespace(ssh_user="tester", ip="10.254.0.11"),
        settings,
        str(tmp_path / "status.json"),
    )
    assert "ConnectTimeout=23" in observed["command"]
    assert observed["timeout"] == 41


def test_stop_failure_retries_then_confirms_cancelled(monkeypatch, tmp_path: Path) -> None:
    """停止 SSH 失败时保留 stopping/allocation，下一轮通过存活检查后才进入 cancelled。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path)
    monkeypatch.setattr("app.services.task_service.write_task_cancel_marker", lambda *_: True)
    cancel_response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
    assert cancel_response.json()["data"]["state"] == "cancelling"

    settings = isolated_executor_settings(tmp_path)
    # 新的安全停止协议只有在 launch、PID 和 /proc 启动时钟均匹配时才允许发出 kill。
    write_runtime_identity(settings, task_id, allocation_id)
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)
    stop_attempts = 0

    def stop_fails_once(command, text=True, stderr=None, timeout=None):
        """第一次模拟网络失败，第二次模拟远端脚本已确认进程组不存在。"""
        nonlocal stop_attempts
        if "expected_start=" not in command[-1]:
            # 停止成功后 executor 会复读同 launch 状态；此处模拟没有自然退出返回码。
            return ""
        stop_attempts += 1
        if stop_attempts == 1:
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)
        return "NebulaGrid stop verification succeeded\n"

    monkeypatch.setattr(task_executor.subprocess, "check_output", stop_fails_once)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task_after_failure = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation_after_failure = db.get(TaskAllocation, allocation_id)
        guard_after_failure = db.scalar(
            select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task_after_failure.id)
        ) if task_after_failure else None
        assert task_after_failure is not None
        assert task_after_failure.state == "cancelling"
        assert task_after_failure.finished_at is None
        assert allocation_after_failure is not None
        assert allocation_after_failure.released_at is None
        assert guard_after_failure is not None
        assert guard_after_failure.state == "cancel_failed"

        task_executor.collect_remote_status(db, task_after_failure, settings)
        db.commit()

    with SessionLocal() as db:
        task_after_success = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation_after_success = db.get(TaskAllocation, allocation_id)
        guard_after_success = db.scalar(
            select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task_after_success.id)
        ) if task_after_success else None

    assert stop_attempts == 2
    assert task_after_success is not None
    assert task_after_success.state == "cancelled"
    assert task_after_success.finished_at is not None
    assert allocation_after_success is not None
    assert allocation_after_success.released_at is not None
    assert guard_after_success is not None
    assert guard_after_success.state == "cancelled"

    running_items = client.get(
        f"/api/tasks?state=running&search={task_id}&page_size=20",
        headers=headers,
    ).json()["data"]["items"]
    history_items = client.get(
        f"/api/tasks?state=history&search={task_id}&page_size=20",
        headers=headers,
    ).json()["data"]["items"]
    assert running_items == []
    assert [item["task_id"] for item in history_items] == [task_id]


def test_stale_launch_status_cannot_finish_current_cancelling_task(monkeypatch, tmp_path: Path) -> None:
    """旧 allocation 的完成文件即使包含成功返回码，也不能结束或释放当前 launch。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path, root_pid=None)
    monkeypatch.setattr("app.services.task_service.write_task_cancel_marker", lambda *_: True)
    cancel_response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
    assert cancel_response.json()["data"]["state"] == "cancelling"

    settings = isolated_executor_settings(tmp_path)
    stale_status = json.dumps(
        {"launch_id": allocation_id + 1000, "state": "finished", "return_code": 0},
    )
    status_path = Path(task_executor.runtime_status_path(settings, task_id))
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(stale_status, encoding="utf-8")
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: False)
    monkeypatch.setattr(task_executor, "read_remote_status", lambda *_: stale_status)

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id)) if task else None

    assert task is not None
    assert task.state == "cancelling"
    assert task.return_code is None
    assert task.finished_at is None
    assert allocation is not None
    assert allocation.released_at is None
    assert guard is not None
    assert guard.state == "cancel_failed"


def test_missing_runtime_and_status_never_prove_remote_process_stopped(monkeypatch, tmp_path: Path) -> None:
    """即使停止标记可写，控制文件缺失也不能把有历史 PID 的运行任务提前归档。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path)
    monkeypatch.setattr("app.services.task_service.write_task_cancel_marker", lambda *_: True)
    assert client.post(f"/api/tasks/{task_id}/cancel", headers=headers).json()["data"]["state"] == "cancelling"

    settings = isolated_executor_settings(tmp_path)
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)
    monkeypatch.setattr(task_executor, "read_remote_status", lambda *_: "")
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
    assert task is not None
    assert task.state == "cancelling"
    assert task.finished_at is None
    assert allocation is not None
    assert allocation.released_at is None


def test_runner_return_cannot_revive_a_concurrently_cancelled_task(monkeypatch, tmp_path: Path) -> None:
    """SSH 回执到达前已提交的 cancelling 必须保留，同时把 PID 写入 guard 供下一轮安全回收。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path, state="starting", root_pid=None)
    settings = isolated_executor_settings(tmp_path)
    monkeypatch.setattr(task_executor, "resolve_user_visible_path", lambda *_: tmp_path)

    def finish_ssh_after_concurrent_cancel(*_args, **_kwargs):
        with SessionLocal() as concurrent_db:
            concurrent_task = concurrent_db.scalar(select(Task).where(Task.task_id == task_id))
            assert concurrent_task is not None
            concurrent_task.state = "cancelling"
            concurrent_task.finished_at = None
            concurrent_task.last_block_reason = "用户已请求停止，正在确认远端进程退出"
            concurrent_guard = concurrent_db.scalar(
                select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == concurrent_task.id)
            )
            assert concurrent_guard is not None
            concurrent_guard.state = "cancelling"
            concurrent_db.commit()
        return json.dumps(
            {
                "task_id": task_id,
                "launch_id": allocation_id,
                "state": "running",
                "pid": 4321,
                "pgid": 4321,
                "process_start_time": 987654,
                "boot_id": "test-node-boot-id",
            }
        )

    monkeypatch.setattr(task_executor, "run_remote_runner", finish_ssh_after_concurrent_cancel)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.start_remote_task(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id)) if task else None
    assert task is not None
    assert task.state == "cancelling"
    assert allocation is not None and allocation.released_at is None
    assert guard is not None
    assert guard.root_pid == 4321
    assert guard.process_group_id == 4321
    assert guard.state == "cancelling"


def test_remote_runner_honors_cancel_before_popen(monkeypatch, tmp_path: Path, capsys) -> None:
    """Popen 前看到同 launch 停止标记时写明确停止回执，绝不能创建用户进程。"""
    args = SimpleNamespace(
        workdir=str(tmp_path),
        command="python train.py",
        log_path=str(tmp_path / "task.log"),
        runtime_path=str(tmp_path / "task.json"),
        status_path=str(tmp_path / "task.status.json"),
        cancel_path=str(tmp_path / "task.cancel.json"),
        task_id="TASK-RUNNER-PRE-CANCEL",
        launch_id=41,
        cuda_visible_devices="0",
    )
    monkeypatch.setattr(remote_runner, "parse_args", lambda: args)
    monkeypatch.setattr(remote_runner, "cancellation_requested", lambda *_: True)

    def unexpected_popen(*_args, **_kwargs):
        raise AssertionError("Popen must not run after a matching pre-launch cancel marker")

    monkeypatch.setattr(remote_runner.subprocess, "Popen", unexpected_popen)
    remote_runner.main()

    status = json.loads(Path(args.status_path).read_text(encoding="utf-8"))
    output = json.loads(capsys.readouterr().out.strip())
    assert status["launch_id"] == 41
    assert status["process_stopped"] is True
    assert output["state"] == "cancelled"


def test_remote_runner_preserves_explicit_return_code_during_post_popen_cancel(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """极短任务先写出的真实退出码优先于 Popen 后到达的停止标记，runner 不得覆盖 status。"""
    args = SimpleNamespace(
        workdir=str(tmp_path),
        command="exit 9",
        log_path=str(tmp_path / "task.log"),
        runtime_path=str(tmp_path / "task.json"),
        status_path=str(tmp_path / "task.status.json"),
        cancel_path=str(tmp_path / "task.cancel.json"),
        task_id="TASK-RUNNER-RC-RACE",
        launch_id=42,
        cuda_visible_devices="",
    )

    class FakeProcess:
        pid = 4321

    checks = 0

    def cancellation_requested(_path, _launch_id):
        nonlocal checks
        checks += 1
        if checks == 3:
            Path(args.status_path).write_text(
                json.dumps(
                    {
                        "task_id": args.task_id,
                        "launch_id": args.launch_id,
                        "state": "finished",
                        "return_code": 9,
                    }
                ),
                encoding="utf-8",
            )
            return True
        return False

    monkeypatch.setattr(remote_runner, "parse_args", lambda: args)
    monkeypatch.setattr(remote_runner, "cancellation_requested", cancellation_requested)
    monkeypatch.setattr(remote_runner, "read_boot_id", lambda: "test-node-boot-id")
    monkeypatch.setattr(remote_runner, "read_process_start_time", lambda *_: 987654)
    monkeypatch.setattr(remote_runner.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(remote_runner.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        remote_runner,
        "terminate_process_group",
        lambda *_: (_ for _ in ()).throw(AssertionError("completed task must not be killed")),
    )
    remote_runner.main()

    status = json.loads(Path(args.status_path).read_text(encoding="utf-8"))
    output = json.loads(capsys.readouterr().out.strip())
    assert checks == 3
    assert status["return_code"] == 9
    assert output["return_code"] == 9


def test_remote_runner_missing_start_time_fails_and_recovers_spawned_group(monkeypatch, tmp_path: Path, capsys) -> None:
    """Popen 后读不到 /proc 启动时钟时不能返回 running，必须立即回收并写无返回码失败回执。"""
    args = SimpleNamespace(
        workdir=str(tmp_path),
        command="python train.py",
        log_path=str(tmp_path / "task.log"),
        runtime_path=str(tmp_path / "task.json"),
        status_path=str(tmp_path / "task.status.json"),
        cancel_path=str(tmp_path / "task.cancel.json"),
        task_id="TASK-RUNNER-NO-STARTTIME",
        launch_id=43,
        cuda_visible_devices="0",
    )

    class FakeProcess:
        pid = 4321

    recovered: list[int] = []
    monkeypatch.setattr(remote_runner, "parse_args", lambda: args)
    monkeypatch.setattr(remote_runner, "cancellation_requested", lambda *_: False)
    monkeypatch.setattr(remote_runner, "read_boot_id", lambda: "test-node-boot-id")
    monkeypatch.setattr(remote_runner, "read_process_start_time", lambda *_: None)
    monkeypatch.setattr(remote_runner.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(remote_runner.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        remote_runner,
        "terminate_process_group",
        lambda process: recovered.append(process.pid) or True,
    )

    with pytest.raises(SystemExit):
        remote_runner.main()

    runtime = json.loads(Path(args.runtime_path).read_text(encoding="utf-8"))
    output = json.loads(capsys.readouterr().out.strip())
    assert recovered == [4321]
    assert runtime["state"] == "launch_failed"
    assert runtime["return_code"] is None
    assert runtime["process_stopped"] is True
    assert output["state"] == "launch_failed"
    # Popen 后 status 归 wrapper 独占；runner 只写 runtime，避免覆盖并发落盘的明确用户返回码。
    assert not Path(args.status_path).exists()


def test_remote_runner_exception_preserves_existing_explicit_return_code(monkeypatch, tmp_path: Path, capsys) -> None:
    """runner 启动后自身异常时，若 wrapper 已有明确返回码，不得再杀进程或覆盖 status。"""
    args = SimpleNamespace(
        workdir=str(tmp_path),
        command="exit 31",
        log_path=str(tmp_path / "task.log"),
        runtime_path=str(tmp_path / "task.json"),
        status_path=str(tmp_path / "task.status.json"),
        cancel_path=str(tmp_path / "task.cancel.json"),
        task_id="TASK-RUNNER-EXPLICIT-ON-ERROR",
        launch_id=44,
        cuda_visible_devices="",
    )

    class FakeProcess:
        pid = 4322

    def fail_after_wrapper_finished(_pid: int):
        Path(args.status_path).write_text(
            json.dumps(
                {
                    "task_id": args.task_id,
                    "launch_id": args.launch_id,
                    "state": "finished",
                    "return_code": 31,
                }
            ),
            encoding="utf-8",
        )
        raise OSError("simulated runner bookkeeping failure")

    monkeypatch.setattr(remote_runner, "parse_args", lambda: args)
    monkeypatch.setattr(remote_runner, "cancellation_requested", lambda *_: False)
    monkeypatch.setattr(remote_runner, "read_boot_id", lambda: "test-node-boot-id")
    monkeypatch.setattr(remote_runner, "read_process_start_time", fail_after_wrapper_finished)
    monkeypatch.setattr(remote_runner.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(remote_runner.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        remote_runner,
        "terminate_process_group",
        lambda *_: (_ for _ in ()).throw(AssertionError("explicit completion must not be killed")),
    )

    remote_runner.main()

    status = json.loads(Path(args.status_path).read_text(encoding="utf-8"))
    output = json.loads(capsys.readouterr().out.strip())
    assert status["return_code"] == 31
    assert output["return_code"] == 31


def test_wrapper_infrastructure_failure_never_becomes_user_return_code(tmp_path: Path) -> None:
    """wrapper 协议只在 process.wait 成功后写整数码，转发器异常必须写 null 并等待 executor 回收。"""
    args = SimpleNamespace(
        workdir=str(tmp_path),
        command="python train.py",
        log_path=str(tmp_path / "task.log"),
        runtime_path=str(tmp_path / "task.json"),
        status_path=str(tmp_path / "task.status.json"),
        cancel_path=str(tmp_path / "task.cancel.json"),
        task_id="TASK-WRAPPER-PROTOCOL",
        launch_id=45,
        cuda_visible_devices="0",
    )
    wrapper = remote_runner.build_wrapper(args)
    assert '"state": "wrapper_failed"' in wrapper
    assert '"return_code": None' in wrapper
    assert "NEBULAGRID_RETURN_CODE" not in wrapper
    assert 'if [ "$relay_code" -ne 0 ]' in wrapper
    assert "wrapper relay failed before a user return code was recorded" in wrapper
    assert "completed_return_code = process.poll()" in wrapper


def test_wrapper_failure_enters_two_phase_cleanup_without_releasing_allocation(monkeypatch, tmp_path: Path) -> None:
    """wrapper_failed 没有用户返回码时必须从 running 转为 stopping，而不是直接失败并释放资源。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path)
    settings = isolated_executor_settings(tmp_path)
    failure_status = json.dumps(
        {
            "task_id": task_id,
            "launch_id": allocation_id,
            "state": "wrapper_failed",
            "return_code": None,
            "process_stopped": False,
            "error": "simulated PTY relay failure",
        }
    )
    monkeypatch.setattr(task_executor, "read_remote_status", lambda *_: failure_status)

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
    assert task is not None and task.state == "cancelling"
    assert task.return_code is None
    assert task.finished_at is None
    assert task.last_block_reason.startswith(task_executor.AUTO_STOP_REASON_PREFIX)
    assert allocation is not None and allocation.released_at is None


def test_runtime_guard_violation_uses_confirmed_two_phase_cleanup(monkeypatch, tmp_path: Path) -> None:
    """GPU 越权只先写 stopping；executor 确认进程退出后才归档 alloc_error 并释放资源。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path)

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        assert task is not None and allocation is not None
        node = db.get(Node, allocation.node_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
        assert node is not None and guard is not None
        runtime_guard.begin_alloc_error_cleanup(
            db,
            task,
            allocation,
            node,
            guard,
            {"GPU-unexpected"},
            {"GPU-allowed"},
        )
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
    assert task is not None and task.state == "cancelling"
    assert task.last_block_reason.startswith("Runtime Guard 检测到任务使用未分配 GPU")
    assert allocation is not None and allocation.released_at is None

    settings = isolated_executor_settings(tmp_path)
    write_runtime_identity(settings, task_id, allocation_id)
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)
    monkeypatch.setattr(task_executor.subprocess, "check_output", lambda *_args, **_kwargs: "stop confirmed")
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
    assert task is not None and task.state == "alloc_error"
    assert task.finished_at is not None
    assert allocation is not None and allocation.released_at is not None


def test_runtime_guard_explicit_return_code_finishes_without_stop(monkeypatch, tmp_path: Path) -> None:
    """越权清理期间若已有真实返回码，则保留 alloc_error 分类并直接完成，不再发送终止信号。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        assert task is not None and allocation is not None
        node = db.get(Node, allocation.node_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
        assert node is not None and guard is not None
        runtime_guard.begin_alloc_error_cleanup(db, task, allocation, node, guard, {"GPU-other"}, set())
        db.commit()

    settings = isolated_executor_settings(tmp_path)
    status_path = Path(task_executor.runtime_status_path(settings, task_id))
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "launch_id": allocation_id,
                "state": "finished",
                "return_code": 12,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)
    monkeypatch.setattr(
        task_executor,
        "stop_remote_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("explicit return code must win")),
    )
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
    assert task is not None and task.state == "alloc_error"
    assert task.return_code == 12
    assert allocation is not None and allocation.released_at is not None


@pytest.mark.parametrize("legacy_state", ["cancelled", "alloc_error"])
def test_legacy_terminal_with_open_allocation_is_restored_for_confirmation(
    legacy_state: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """旧版取消/越权终态即使 guard 同名，也必须因开放 allocation 恢复到 stopping。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(
        client,
        headers,
        tmp_path,
        state=legacy_state,
        root_pid=None,
    )
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
        assert guard is not None
        # 保留旧版可能已经写下的 cancelled/alloc_error，证明新逻辑不把它误当作可靠退出回执。
        guard.state = legacy_state
        assert release_terminal_allocations(db) == 0
        db.commit()

    resubmit_response = client.post(f"/api/tasks/{task_id}/resubmit", headers=headers)
    assert resubmit_response.status_code == 422

    with SessionLocal() as db:
        assert task_executor.restore_unconfirmed_legacy_cancellations(db) == 1
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id)) if task else None
    assert task is not None and task.state == "cancelling"
    assert task.finished_at is None
    if legacy_state == "alloc_error":
        assert task.last_block_reason.startswith("Runtime Guard 检测到任务使用未分配 GPU")
    assert allocation is not None and allocation.released_at is None
    assert guard is not None and guard.state == "cancelling"
    # executor 已恢复为 cancelling 后，API 同样必须拒绝立即重提，直到停止确认和资源释放完成。
    assert client.post(f"/api/tasks/{task_id}/resubmit", headers=headers).status_code == 422

    settings = isolated_executor_settings(tmp_path)
    status_path = Path(task_executor.runtime_status_path(settings, task_id))
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "launch_id": allocation_id,
                "state": "cancelled",
                "return_code": None,
                "process_stopped": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
    assert task is not None and task.state == legacy_state
    assert allocation is not None and allocation.released_at is not None
    assert client.post(f"/api/tasks/{task_id}/resubmit", headers=headers).status_code == 200


def test_admin_node_and_settings_smoke() -> None:
    """验证管理员节点登记和配置读写接口符合统一响应格式。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    node_name = f"node-a-{uuid4().hex[:8]}"

    node_response = client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": node_name, "ip": "10.0.0.10", "gpu_schedulable_flags": [1]},
    )
    settings_response = client.patch(
        "/api/admin/settings",
        headers=headers,
        json={"values": {"scheduler.enabled": "false"}},
    )

    assert node_response.status_code == 200
    assert node_response.json()["data"]["name"] == node_name
    assert node_response.json()["data"]["gpu_schedulable_flags"] == [1]
    assert settings_response.status_code == 200
    assert settings_response.json()["ok"] is True


def test_monitor_applies_admin_gpu_schedulable_flags() -> None:
    """验证 monitor 扫描 GPU 型号和数量，管理员 0/1 列表只决定是否参与调度。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    node_payload = {
        "name": f"node-flags-{uuid4().hex[:8]}",
        "ip": "10.0.0.31",
        "gpu_schedulable_flags": [1, 0],
        "gpu_compute_capability_overrides": ["9.0", ""],
    }
    node_id = client.post("/api/admin/nodes", headers=headers, json=node_payload).json()["data"]["id"]

    with SessionLocal() as db:
        node = db.get(Node, node_id)
        assert node is not None
        sync_gpu_inventory(
            db,
            node,
            [
                {"index": 0, "uuid": "GPU-visible", "name": "RTX 4090", "memory_total_mb": 24576, "compute_capability": "8.9"},
                {"index": 1, "uuid": "GPU-display", "name": "NVIDIA T400", "memory_total_mb": 4096, "compute_capability": "7.5"},
            ],
        )
        db.commit()

    nodes = client.get("/api/nodes", headers=headers).json()["data"]
    node = next(item for item in nodes if item["id"] == node_id)
    assert [gpu["model"] for gpu in node["gpus"]] == ["RTX 4090", "NVIDIA T400"]
    assert [gpu["schedulable"] for gpu in node["gpus"]] == [True, False]
    assert [gpu["compute_capability"] for gpu in node["gpus"]] == ["9.0", "7.5"]
    assert [gpu["detected_compute_capability"] for gpu in node["gpus"]] == ["8.9", "7.5"]


def test_pytorch_gpu_compatibility_uses_effective_compute_capability() -> None:
    """验证原生/同主版本状态、compute 忽略策略和管理员算力覆盖值。"""
    env = SimpleNamespace(pytorch_version="2.1.0", pytorch_arch_list=["sm_50", "sm_75", "sm_86", "compute_86"])
    gpu = SimpleNamespace(gpu_index=0, compute_capability="8.9")
    node = SimpleNamespace(gpu_compute_capability_overrides=["8.6"])

    assert pytorch_gpu_compatibility(env, node, gpu) == "native_supported"
    node.gpu_compute_capability_overrides = []
    assert pytorch_gpu_compatibility(env, node, gpu) == "same_major_compatible"
    gpu.compute_capability = "9.0"
    # compute_86 不参与判断，跨主版本的 9.0 仍属于不支持。
    assert pytorch_gpu_compatibility(env, node, gpu) == "unsupported"
    gpu.compute_capability = "3.7"
    assert pytorch_gpu_compatibility(env, node, gpu) == "unsupported"
    env.pytorch_arch_list.append("compute_37")
    assert pytorch_gpu_compatibility(env, node, gpu) == "unsupported"
    # 带后缀的专用 cubin 只允许数值架构精确命中，不能向同主版本更高次版本扩展。
    env.pytorch_arch_list = ["sm_90a"]
    gpu.compute_capability = "9.0"
    assert pytorch_gpu_compatibility(env, node, gpu) == "native_supported"
    gpu.compute_capability = "9.1"
    assert pytorch_gpu_compatibility(env, node, gpu) == "unsupported"
    env.pytorch_version = None
    assert pytorch_gpu_compatibility(env, node, gpu) == "unknown"


def test_scheduler_filters_unsupported_gpu_unless_model_is_forced() -> None:
    """验证自动模式仅过滤不支持卡，其他状态保持原顺序，显式型号则完全绕过过滤。"""
    suffix = uuid4().hex[:8]
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        env = Env(
            owner_user_id=admin.id,
            name=f"compat-{suffix}",
            path=f"/tmp/compat-{suffix}",
            state="available",
            pytorch_version="2.1.0",
            pytorch_cuda_version="11.8",
            pytorch_arch_list=["sm_86", "compute_86"],
        )
        unsupported_node = Node(
            name=f"compat-unsupported-{suffix}",
            ip="10.20.0.1",
            ssh_user="ddltm",
            state="online",
            scheduling_enabled=True,
            gpu_schedulable_flags=[1],
            gpus=[Gpu(gpu_index=0, gpu_uuid=f"GPU-unsupported-{suffix}", model="Unsupported GPU", total_vram_mb=24576, compute_capability="3.5", schedulable=True)],
        )
        same_major_node = Node(
            name=f"compat-same-major-{suffix}",
            ip="10.20.0.2",
            ssh_user="ddltm",
            state="online",
            scheduling_enabled=True,
            gpu_schedulable_flags=[1],
            gpus=[Gpu(gpu_index=0, gpu_uuid=f"GPU-same-major-{suffix}", model="Compatible GPU", total_vram_mb=24576, compute_capability="8.9", schedulable=True)],
        )
        native_node = Node(
            name=f"compat-native-{suffix}",
            ip="10.20.0.3",
            ssh_user="ddltm",
            state="online",
            scheduling_enabled=True,
            gpu_schedulable_flags=[1],
            gpus=[Gpu(gpu_index=0, gpu_uuid=f"GPU-native-{suffix}", model="Native GPU", total_vram_mb=24576, compute_capability="8.6", schedulable=True)],
        )
        db.add_all([env, unsupported_node, same_major_node, native_node])
        db.commit()

        requirement = SimpleNamespace(need_gpus=1, gpu_types=[], allow_gpu_reuse=False, max_reuse_count=1)
        task = SimpleNamespace(env_id=env.id, requirement=requirement)
        assert select_gpu_allocation(db, [unsupported_node], task, LatestMetrics()) is None
        selected = select_gpu_allocation(db, [unsupported_node, same_major_node, native_node], task, LatestMetrics())
        assert selected is not None
        # 不支持节点被跳过后，同主版本节点沿用原有顺序被选中，不再为了原生支持重排到第三个节点。
        assert selected[0].id == same_major_node.id

        requirement.gpu_types = ["Unsupported GPU"]
        forced = select_gpu_allocation(db, [unsupported_node, same_major_node, native_node], task, LatestMetrics())
        assert forced is not None
        assert forced[0].id == unsupported_node.id

        # 本测试创建的在线节点会影响后续调度用例，断言完成后立即清理隔离数据。
        db.delete(unsupported_node)
        db.delete(same_major_node)
        db.delete(native_node)
        db.delete(env)
        db.commit()


def test_monitor_skips_admin_offline_node_until_reconnect(monkeypatch) -> None:
    """验证强制下线后的 offline 节点不会被监控轮询自动拉回 online。"""
    node = Node(name="paused-node", ip="10.0.0.36", ssh_user="ddltm", state="offline", scheduling_enabled=False)
    calls: list[str] = []

    def fake_fetch_remote_metrics(*args, **kwargs):
        """如果监控暂停生效，这个 SSH 探测替身不应被调用。"""
        calls.append("called")
        return {}

    monkeypatch.setattr("app.workers.node_monitor.fetch_remote_metrics", fake_fetch_remote_metrics)

    monitor_node(None, node, "/remote", "/python")

    assert node_monitor_paused(node) is True
    assert node.state == "offline"
    assert node.scheduling_enabled is False
    assert calls == []


def test_monitor_long_connection_command_uses_remote_loop() -> None:
    """验证节点监控长连接通过远端循环脚本输出，而不是每轮重新执行一次 SSH 命令。"""
    target = NodeMonitorTarget(
        node_id=7,
        name="node-loop",
        ip="10.0.0.37",
        ssh_user="ddltm",
        remote_code_root="/home/ddltm/envs/nebulagrid_remote",
        miniconda_python="/home/ddltm/envs/miniconda3/bin/python",
        interval_seconds=5,
        reconnect_attempts=3,
        watchdog_timeout_seconds=600,
    )

    command = build_remote_monitor_command(target, loop=True)

    assert command[0] == "ssh"
    assert "ServerAliveInterval=10" in command
    assert command[-2] == "ddltm@10.0.0.37"
    assert command[-1] == (
        "/home/ddltm/envs/miniconda3/bin/python -u "
        "/home/ddltm/envs/nebulagrid_remote/monitor.py --loop --interval 5"
    )
    assert target.reconnect_attempts == 3
    assert target.watchdog_timeout_seconds == 600


def test_monitor_watchdog_timeout_when_stream_has_no_payload(monkeypatch) -> None:
    """验证 SSH 连接未退出但长期没有有效 JSON 时，watchdog 会主动触发重连路径。"""

    class SilentStdout:
        """模拟仍保持打开但迟迟不输出完整监控行的 stdout。"""

        def __iter__(self):
            return self

        def __next__(self):
            time.sleep(0.2)
            raise StopIteration

    target = NodeMonitorTarget(
        node_id=8,
        name="node-watchdog",
        ip="10.0.0.38",
        ssh_user="ddltm",
        remote_code_root="/home/ddltm/envs/nebulagrid_remote",
        miniconda_python="/home/ddltm/envs/miniconda3/bin/python",
        interval_seconds=5,
        reconnect_attempts=3,
        watchdog_timeout_seconds=0.05,
    )
    worker = NodeMonitorWorker(target)
    process = SimpleNamespace(stdout=SilentStdout())
    monkeypatch.setattr("app.workers.node_monitor.target_monitor_paused", lambda node_id: False)

    with pytest.raises(MonitorWatchdogTimeout) as exc_info:
        worker.read_monitor_stream(process)

    assert exc_info.value.got_payload is False


def test_scheduler_schedules_one_exclusive_task_per_tick() -> None:
    """验证一轮调度只成功分配一个任务，避免同一张独占 GPU 在同一事务里被重复占用。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    node_id = client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": f"node-exclusive-{uuid4().hex[:8]}", "ip": "10.0.0.32", "gpu_schedulable_flags": [1]},
    ).json()["data"]["id"]

    with SessionLocal() as db:
        db.merge(Setting(key="scheduler.enabled", value="true"))
        node = db.get(Node, node_id)
        assert node is not None
        node.state = "online"
        node.scheduling_enabled = True
        sync_gpu_inventory(
            db,
            node,
            [{"index": 0, "uuid": "GPU-exclusive", "name": "RTX 4090", "memory_total_mb": 24576}],
        )
        db.commit()

    task_ids = []
    for index in range(2):
        response = client.post(
            "/api/tasks",
            headers=headers,
            json={
                "description": f"exclusive-{index}",
                "workdir": "/",
                "command": "python train.py",
                "urgent": True,
                "requirement": {"need_gpus": 1, "node_id": node_id, "allow_gpu_reuse": False},
            },
        )
        task_ids.append(response.json()["data"]["task_id"])

    with SessionLocal() as db:
        db.query(Task).filter(Task.state == "wait").filter(~Task.task_id.in_(task_ids)).update(
            {Task.state: "on_hold", Task.on_hold: True},
            synchronize_session=False,
        )
        db.commit()

    scheduler_tick()

    with SessionLocal() as db:
        tasks = db.scalars(select(Task).where(Task.task_id.in_(task_ids))).all()
        states = {task.task_id: task.state for task in tasks}
        task_pks = [task.id for task in tasks]
        allocations = db.scalars(
            select(TaskAllocation)
            .where(TaskAllocation.task_id.in_(task_pks))
            .where(TaskAllocation.released_at.is_(None))
        ).all()

    assert sorted(states.values()) == ["dispatching", "wait"]
    assert sum(1 for allocation in allocations if allocation.gpu_ids) == 1


def test_force_offline_node_defers_remote_stop_until_after_intent_commit(monkeypatch, tmp_path: Path) -> None:
    """强制下线先提交 cancelling 且不在 HTTP 锁事务中 SSH，executor 确认后才释放 GPU。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    node_id = client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": f"node-force-offline-{uuid4().hex[:8]}", "ip": "10.0.0.35", "gpu_schedulable_flags": [1]},
    ).json()["data"]["id"]

    with SessionLocal() as db:
        db.merge(Setting(key="scheduler.enabled", value="true"))
        node = db.get(Node, node_id)
        assert node is not None
        node.state = "online"
        node.scheduling_enabled = True
        sync_gpu_inventory(
            db,
            node,
            [{"index": 0, "uuid": f"GPU-force-{node_id}", "name": "RTX 4090", "memory_total_mb": 24576}],
        )
        db.commit()

    task_response = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "description": "force-offline-running",
            "workdir": "/",
            "command": "python train.py",
            "urgent": True,
            "requirement": {"need_gpus": 1, "node_id": node_id, "allow_gpu_reuse": False},
        },
    )
    task_id = task_response.json()["data"]["task_id"]

    with SessionLocal() as db:
        db.query(Task).filter(Task.state == "wait").filter(Task.task_id != task_id).update(
            {Task.state: "on_hold", Task.on_hold: True},
            synchronize_session=False,
        )
        db.commit()

    scheduler_tick()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))
        assert guard is not None
        task.state = "running"
        guard.root_pid = 4321
        guard.process_group_id = 4321
        guard.state = "running"
        db.commit()

    marker_calls: list[tuple[str, int]] = []

    def record_force_offline_marker(task_id_value: str, launch_id: int) -> bool:
        """NodeService 写外部标记前，数据库停止意图必须已对另一会话可见。"""
        with SessionLocal() as verify_db:
            persisted = verify_db.scalar(select(Task).where(Task.task_id == task_id_value))
            assert persisted is not None and persisted.state == "cancelling"
        marker_calls.append((task_id_value, launch_id))
        return True

    monkeypatch.setattr("app.services.task_service.write_task_cancel_marker", record_force_offline_marker)
    offline_response = client.post(f"/api/admin/nodes/{node_id}/force-offline", headers=headers)

    with SessionLocal() as db:
        node = db.get(Node, node_id)
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        allocation = db.scalar(select(TaskAllocation).where(TaskAllocation.task_id == task.id))
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))

    assert offline_response.status_code == 200
    assert offline_response.json()["data"]["state"] == "offline"
    assert offline_response.json()["data"]["gpus"][0]["scheduled_occupied"] is True
    assert node is not None
    assert node.state == "offline"
    assert node.scheduling_enabled is False
    assert task.state == "cancelling"
    assert task.finished_at is None
    assert allocation is not None
    assert allocation.released_at is None
    assert guard is not None
    assert guard.state == "cancelling"
    assert marker_calls == [(task_id, allocation.id)]

    settings = isolated_executor_settings(tmp_path)
    write_runtime_identity(settings, task_id, allocation.id)
    ssh_commands: list[list[str]] = []

    def fake_check_output(command, text=True, stderr=None, timeout=None):
        """executor 的远端终止命令返回成功，代表存活复核已经确认进程组退出。"""
        ssh_commands.append(list(command))
        return "NebulaGrid stop verification succeeded\n"

    monkeypatch.setattr(task_executor, "write_task_cancel_marker", lambda *_: True)
    monkeypatch.setattr(task_executor.subprocess, "check_output", fake_check_output)
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        node = db.get(Node, node_id)
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        allocation = db.scalar(select(TaskAllocation).where(TaskAllocation.task_id == task.id))
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))

    assert task.state == "cancelled"
    assert task.last_block_reason == ""
    assert allocation is not None
    assert allocation.released_at is not None
    assert guard is not None
    assert guard.state == "cancelled"
    assert ssh_commands
    assert "pgid=4321" in ssh_commands[0][-1]
    assert "expected_start=987654" in ssh_commands[0][-1]
    assert 'kill -KILL -"$pgid"' in ssh_commands[0][-1]


@pytest.mark.parametrize("runtime_case", ["missing", "stale_launch", "missing_boot_id"])
def test_force_offline_with_unverified_runtime_keeps_cancelling_and_allocation(
    monkeypatch,
    tmp_path: Path,
    runtime_case: str,
) -> None:
    """缺失或属于旧 launch 的进程身份不能触发 kill，也不能提前释放调度占用。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    task_id, allocation_id = create_remote_task_fixture(client, headers, tmp_path)

    with SessionLocal() as db:
        allocation = db.get(TaskAllocation, allocation_id)
        assert allocation is not None
        node_id = allocation.node_id

    settings = isolated_executor_settings(tmp_path)
    monkeypatch.setattr("app.services.task_service.write_task_cancel_marker", lambda *_: True)
    if runtime_case == "stale_launch":
        write_runtime_identity(settings, task_id, allocation_id, launch_id=allocation_id + 1)
    elif runtime_case == "missing_boot_id":
        write_runtime_identity(settings, task_id, allocation_id, boot_id="")

    ssh_commands: list[list[str]] = []

    def capture_unexpected_ssh(command, text=True, stderr=None, timeout=None):
        """记录任何越过身份校验的 SSH 调用，确保测试不会通过异常吞噬误杀行为。"""
        ssh_commands.append(list(command))
        return ""

    monkeypatch.setattr(task_executor.subprocess, "check_output", capture_unexpected_ssh)
    offline_response = client.post(f"/api/admin/nodes/{node_id}/force-offline", headers=headers)

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        task_executor.collect_remote_status(db, task, settings)
        db.commit()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        allocation = db.get(TaskAllocation, allocation_id)
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id)) if task else None

    assert offline_response.status_code == 200
    assert offline_response.json()["data"]["state"] == "offline"
    assert ssh_commands
    assert all("kill -TERM" not in command[-1] and "kill -KILL" not in command[-1] for command in ssh_commands)
    assert task is not None
    assert task.state == "cancelling"
    assert task.finished_at is None
    assert allocation is not None
    assert allocation.released_at is None
    assert guard is not None
    assert guard.state == "cancel_failed"


def test_scheduler_combines_gpu_model_and_node_constraints() -> None:
    """验证 GPU 型号和指定节点是组合约束：只选型号看所有可见节点，同时选节点时只看该节点。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    suffix = uuid4().hex[:8]
    small_node_id = client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": f"node-model-small-{suffix}", "ip": "10.0.0.33", "gpu_schedulable_flags": [1]},
    ).json()["data"]["id"]
    large_node_id = client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": f"node-model-large-{suffix}", "ip": "10.0.0.34", "gpu_schedulable_flags": [1]},
    ).json()["data"]["id"]

    with SessionLocal() as db:
        db.merge(Setting(key="scheduler.enabled", value="true"))
        small_node = db.get(Node, small_node_id)
        large_node = db.get(Node, large_node_id)
        assert small_node is not None
        assert large_node is not None
        small_node.state = "online"
        small_node.scheduling_enabled = True
        large_node.state = "online"
        large_node.scheduling_enabled = True
        sync_gpu_inventory(
            db,
            small_node,
            [{"index": 0, "uuid": f"GPU-small-{suffix}", "name": "RTX 3060", "memory_total_mb": 12288}],
        )
        sync_gpu_inventory(
            db,
            large_node,
            [{"index": 0, "uuid": f"GPU-large-{suffix}", "name": "RTX 4090", "memory_total_mb": 24576}],
        )
        db.commit()

    first_response = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "description": "model-constraint-first",
            "workdir": "/",
            "command": "python train.py",
            "urgent": True,
            "requirement": {"need_gpus": 1, "gpu_types": ["RTX 4090"], "allow_gpu_reuse": False},
        },
    )
    first_task_id = first_response.json()["data"]["task_id"]
    first_requirement = first_response.json()["data"]["requirement"]
    assert first_requirement["gpu_types"] == ["RTX 4090"]
    assert first_requirement["node_id"] is None

    with SessionLocal() as db:
        db.query(Task).filter(Task.state == "wait").filter(Task.task_id != first_task_id).update(
            {Task.state: "on_hold", Task.on_hold: True},
            synchronize_session=False,
        )
        db.commit()

    scheduler_tick()

    with SessionLocal() as db:
        first_task = db.scalar(select(Task).where(Task.task_id == first_task_id))
        assert first_task is not None
        first_allocation = db.scalar(select(TaskAllocation).where(TaskAllocation.task_id == first_task.id))
        assert first_allocation is not None
        assert first_allocation.node_id == large_node_id

    second_response = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "description": "model-constraint-second",
            "workdir": "/",
            "command": "python train.py",
            "urgent": True,
            "requirement": {
                "need_gpus": 1,
                "node_id": small_node_id,
                "gpu_types": ["RTX 4090"],
                "allow_gpu_reuse": False,
            },
        },
    )
    second_task_id = second_response.json()["data"]["task_id"]

    with SessionLocal() as db:
        db.query(Task).filter(Task.state == "wait").filter(Task.task_id != second_task_id).update(
            {Task.state: "on_hold", Task.on_hold: True},
            synchronize_session=False,
        )
        db.commit()

    scheduler_tick()

    with SessionLocal() as db:
        second_task = db.scalar(select(Task).where(Task.task_id == second_task_id))
        assert second_task is not None
        second_allocation = db.scalar(select(TaskAllocation).where(TaskAllocation.task_id == second_task.id))

    assert second_task.state == "wait"
    assert second_allocation is None
    assert second_task.last_block_reason


def test_waiting_task_info_prefers_requirement_over_old_allocation() -> None:
    """验证等待任务列表展示当前需求节点，而不是历史 allocation 的旧节点。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}
    suffix = uuid4().hex[:8]
    old_node_id = client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": f"node-old-allocation-{suffix}", "ip": "10.0.0.36", "gpu_schedulable_flags": [1]},
    ).json()["data"]["id"]
    requested_node_id = client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": f"node-current-request-{suffix}", "ip": "10.0.0.37", "gpu_schedulable_flags": [1]},
    ).json()["data"]["id"]
    task_response = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "description": "display-current-requirement",
            "workdir": "/",
            "command": "python train.py",
            "requirement": {"need_gpus": 1, "node_id": requested_node_id, "gpu_types": ["RTX 4090"]},
        },
    )
    task_id = task_response.json()["data"]["task_id"]

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        db.add(
            TaskAllocation(
                task_id=task.id,
                node_id=old_node_id,
                gpu_ids=[],
                allocation_mode="cpu",
                released_at=local_datetime(),
            )
        )
        db.commit()

    wait_tasks = client.get("/api/tasks?state=wait&page_size=20", headers=headers).json()["data"]["items"]
    item = next(task for task in wait_tasks if task["task_id"] == task_id)
    assert item["node_id"] == requested_node_id
    assert item["requirement"]["node_id"] == requested_node_id
    assert item["requirement"]["gpu_types"] == ["RTX 4090"]


def test_scheduler_interval_allows_subsecond_setting() -> None:
    """验证调度间隔支持 0.5 秒这类小数，便于单实例调度器提高响应频率。"""
    with SessionLocal() as db:
        db.merge(Setting(key="scheduler.interval_seconds", value="0.5"))
        db.commit()
    with SessionLocal() as db:
        assert scheduler_interval_seconds(db) == 0.5


def test_monitor_reconnect_attempts_uses_admin_setting() -> None:
    """验证节点监控自动重连次数来自管理员系统设置，默认用于限制断线后的重试范围。"""
    with SessionLocal() as db:
        db.merge(Setting(key="monitor.reconnect_attempts", value="4"))
        db.commit()
    with SessionLocal() as db:
        assert monitor_reconnect_attempts(db) == 4
    assert normalize_reconnect_attempts("-1") == 0
    assert normalize_reconnect_attempts("999") == 100


def test_monitor_watchdog_timeout_uses_admin_setting() -> None:
    """验证管理员后台系统设置可以覆盖节点监控无输出 watchdog 超时。"""
    with SessionLocal() as db:
        db.merge(Setting(key="monitor.watchdog_timeout_seconds", value="42"))
        db.commit()
        assert monitor_watchdog_timeout_seconds(db) == 42
    assert normalize_watchdog_timeout("bad") == 600
    assert normalize_watchdog_timeout("-1") == 1
    assert normalize_watchdog_timeout("999999") == 86400


def test_scheduler_prefers_user_owned_then_group_then_public_nodes() -> None:
    """验证任务在所有可见节点里优先使用本人节点，其后才考虑组内和公开节点。"""
    client = make_client()
    admin_token = login_as_admin(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    suffix = uuid4().hex[:8]

    mentor = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": f"sched-mentor-{suffix}",
            "real_name": "Scheduler Mentor",
            "role": "mentor",
            "state": "enabled",
            "password": "mentor123",
        },
    ).json()["data"]
    student = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": f"sched-student-{suffix}",
            "real_name": "Scheduler Student",
            "role": "student",
            "state": "enabled",
            "password": "student123",
            "supervisor_ids": [mentor["id"]],
        },
    ).json()["data"]
    peer = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": f"sched-peer-{suffix}",
            "real_name": "Scheduler Peer",
            "role": "student",
            "state": "enabled",
            "password": "student123",
            "supervisor_ids": [mentor["id"]],
        },
    ).json()["data"]

    def create_node(label: str, owner_ids: list[int], access_scope: str, sharing_scope: str) -> int:
        response = client.post(
            "/api/admin/nodes",
            headers=admin_headers,
            json={
                "name": f"sched-{label}-{suffix}",
                "ip": f"10.0.1.{len(label) + len(owner_ids) + 10}",
                "gpu_schedulable_flags": [1],
                "owner_user_ids": owner_ids,
                "access_scope": access_scope,
                "sharing_scope": sharing_scope,
            },
        )
        return response.json()["data"]["id"]

    public_node_id = create_node("public", [], "public", "public")
    peer_public_private_node_id = create_node("peer-public-private", [peer["id"]], "private", "public")
    group_node_id = create_node("group", [mentor["id"]], "private", "group")
    own_node_id = create_node("own", [student["id"]], "private", "none")
    node_ids = [public_node_id, peer_public_private_node_id, group_node_id, own_node_id]

    with SessionLocal() as db:
        db.merge(Setting(key="scheduler.enabled", value="true"))
        for node_id in node_ids:
            node = db.get(Node, node_id)
            assert node is not None
            node.state = "online"
            node.scheduling_enabled = True
            sync_gpu_inventory(
                db,
                node,
                [{"index": 0, "uuid": f"GPU-{node_id}", "name": "RTX 4090", "memory_total_mb": 24576}],
            )
        db.commit()

    student_token = login_user(client, student["username"], "student123")
    student_headers = {"Authorization": f"Bearer {student_token}"}
    task_response = client.post(
        "/api/tasks",
        headers=student_headers,
        json={
            "description": "priority-order",
            "workdir": "/",
            "command": "python train.py",
            "urgent": True,
            "requirement": {"need_gpus": 1, "allow_gpu_reuse": False},
        },
    )
    task_id = task_response.json()["data"]["task_id"]

    with SessionLocal() as db:
        db.query(Task).filter(Task.state == "wait").filter(Task.task_id != task_id).update(
            {Task.state: "on_hold", Task.on_hold: True},
            synchronize_session=False,
        )
        db.commit()

    scheduler_tick()

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        allocation = db.scalar(select(TaskAllocation).where(TaskAllocation.task_id == task.id))

    assert task.state == "dispatching"
    assert allocation is not None
    assert allocation.node_id == own_node_id


def login_as_admin(client: TestClient) -> str:
    """登录演示管理员账号，并返回后续测试可复用的 Bearer token。"""
    response = client.post(
        "/api/auth/login",
        json={"identity": "admin", "password": "admin123"},
    )
    return response.json()["data"]["access_token"]


def test_login_session_status_has_dedicated_display_fields() -> None:
    """验证登录会话状态返回独立展示字段，避免前端把 offline 误显示成节点掉线。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}

    list_response = client.post("/api/auth/sessions/list", headers=headers)
    session = list_response.json()["data"][0]
    offline_response = client.post("/api/auth/sessions/offline", headers=headers, json={"session_id": session["id"]})
    offline_session = offline_response.json()["data"]

    assert list_response.status_code == 200
    assert "state" not in session
    assert session["session_state"] == "online"
    assert session["status_label"] == "在线"
    assert session["status_category"] == "online"
    assert offline_response.status_code == 200
    assert "state" not in offline_session
    assert offline_session["session_state"] == "offline"
    assert offline_session["status_label"] == "已下线"
    assert offline_session["status_category"] == "offline"


def test_envs_are_globally_usable_but_owner_modified() -> None:
    """验证环境可被所有用户选择使用，但包安装和删除等修改动作只允许环境所有者执行。"""
    client = make_client()
    admin_token = login_as_admin(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    create_user_response = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "env-student",
            "real_name": "Env Student",
            "role": "student",
            "state": "enabled",
            "password": "student123",
        },
    )
    student_token = login_user(client, "env-student", "student123")
    student_headers = {"Authorization": f"Bearer {student_token}"}

    env_response = client.post(
        "/api/envs/register",
        headers=admin_headers,
        json={"name": "shared-torch", "python_version": "3.11"},
    )
    env = env_response.json()["data"]
    task_response = client.post(
        "/api/tasks",
        headers=student_headers,
        json={"command": "python train.py", "workdir": "/", "env_id": env["id"]},
    )
    forbidden_upload = client.post(
        f"/api/envs/{env['id']}/packages/upload",
        headers=student_headers,
        json={"filename": "pkg.whl"},
    )

    assert create_user_response.status_code == 200
    assert env_response.status_code == 200
    assert env["path"] == "/home/ddltm/envs/miniconda3/envs/shared-torch"
    assert client.get("/api/envs", headers=student_headers).json()["data"][0]["can_modify"] is False
    assert task_response.status_code == 200
    assert forbidden_upload.status_code == 403


def test_env_name_cannot_create_nested_conda_directory() -> None:
    """验证环境名只能映射为 miniconda envs 下的一级目录，避免 conda 无法识别。"""
    client = make_client()
    token = login_as_admin(client)
    response = client.post(
        "/api/envs/register",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "owner/nested"},
    )

    assert response.status_code == 422


def test_user_management_maps_linux_accounts_and_deletes_children() -> None:
    """验证管理员映射主账户，教学用户映射子账户，删除用户后账号不可再登录。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}

    admin_list = client.get("/api/users", headers=headers).json()["data"]
    create_response = client.post(
        "/api/users",
        headers=headers,
        json={
            "username": "delete-student",
            "real_name": "Delete Student",
            "role": "student",
            "state": "enabled",
            "password": "student123",
        },
    )
    created = create_response.json()["data"]
    delete_response = client.delete(f"/api/users/{created['id']}", headers=headers)
    login_response = client.post(
        "/api/auth/login",
        json={"identity": "delete-student", "password": "student123"},
    )

    assert admin_list[0]["linux_account_name"] == "ddltm"
    assert admin_list[0]["home_path"] == "/home/ddltm"
    assert create_response.status_code == 200
    assert created["linux_account_name"] == "delete-student"
    assert created["home_path"] == "/home/ddltm/data/user/delete-student"
    assert delete_response.status_code == 200
    assert login_response.status_code == 401


def test_current_user_can_toggle_samba_with_current_password(monkeypatch) -> None:
    """验证新用户默认关闭 Samba，开启时必须校验当前密码并返回服务状态。"""
    calls: list[tuple[str, str, str | None]] = []

    def fake_ensure(username: str, role: str, password: str | None = None):
        """记录开启 Samba 前的 Linux 子账户补齐动作，避免 smbpasswd 因系统账号不存在失败。"""
        calls.append(("ensure", username, password))

    def fake_enable(account_name: str, password: str):
        """模拟生产环境成功创建并启用 Samba 账号，避免测试改动本机系统账户。"""
        from app.services.samba_service import SambaAccountPlan

        calls.append(("enable", account_name, password))
        return SambaAccountPlan(account_name, True, "enabled", "已启用", True, [], updated_at="now")

    monkeypatch.setattr("app.services.auth_service.ensure_child_account", fake_ensure)
    monkeypatch.setattr("app.services.auth_service.enable_samba_account", fake_enable)

    client = make_client()
    admin_token = login_as_admin(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    create_response = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "samba-student",
            "real_name": "Samba Student",
            "role": "student",
            "state": "enabled",
            "password": "student123",
        },
    )
    student_token = login_user(client, "samba-student", "student123")
    student_headers = {"Authorization": f"Bearer {student_token}"}

    status_response = client.post("/api/auth/samba/status", headers=student_headers)
    missing_password_response = client.post("/api/auth/samba/update", headers=student_headers, json={"enabled": True})
    enable_response = client.post(
        "/api/auth/samba/update",
        headers=student_headers,
        json={"enabled": True, "current_password": "student123"},
    )

    assert create_response.status_code == 200
    assert create_response.json()["data"]["samba_enabled"] is False
    assert create_response.json()["data"]["samba_status"] == "disabled"
    assert status_response.json()["data"]["samba_status"] == "disabled"
    assert missing_password_response.status_code == 401
    assert enable_response.status_code == 200
    assert enable_response.json()["data"]["samba_enabled"] is True
    assert enable_response.json()["data"]["samba_status"] == "enabled"
    assert calls == [
        ("ensure", "samba-student", "student123"),
        ("enable", "samba-student", "student123"),
    ]


def test_main_linux_account_cannot_enable_samba() -> None:
    """验证映射到平台主账户的账号不能开启 Samba，避免 Windows 文件共享暴露 /home/ddltm。"""
    client = make_client()
    token = login_as_admin(client)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/api/auth/samba/update",
        headers=headers,
        json={"enabled": True, "current_password": "admin123"},
    )
    status_response = client.post("/api/auth/samba/status", headers=headers)

    assert response.status_code == 401
    assert status_response.status_code == 200
    assert status_response.json()["data"]["samba_enabled"] is False
    assert status_response.json()["data"]["samba_status"] == "disabled"


def test_samba_account_change_restarts_smbd(monkeypatch) -> None:
    """验证真实 Samba 账号变更后会自动重启 smbd，让 Windows 共享立即读取最新账号状态。"""
    from app.core.config import Settings
    from app.services import samba_service

    calls: list[tuple[list[str], str | None]] = []

    def fake_run(command, input=None, text=True, check=True, capture_output=True):
        """记录外部命令调用，避免测试环境真实执行 smbpasswd 或 systemctl。"""
        calls.append((list(command), input))

    monkeypatch.setattr(samba_service.os, "name", "posix")
    monkeypatch.setattr(samba_service, "samba_management_enabled", lambda settings: True)
    monkeypatch.setattr(samba_service, "privileged_command", lambda command: command)
    monkeypatch.setattr(samba_service.subprocess, "run", fake_run)
    monkeypatch.setattr(
        samba_service,
        "command_path",
        lambda name: {("smbpasswd"): "/usr/bin/smbpasswd", ("systemctl"): "/bin/systemctl"}.get(name, name),
    )

    plan = samba_service.run_samba_commands(
        "samba-student",
        True,
        [["/usr/bin/smbpasswd", "-s", "-a", "samba-student"]],
        Settings(manage_samba_accounts=True),
        password="student123",
    )

    assert plan.executed is True
    assert plan.commands[-1] == ["/bin/systemctl", "restart", "smbd"]
    assert calls == [
        (["/usr/bin/smbpasswd", "-s", "-a", "samba-student"], "student123\nstudent123\n"),
        (["/bin/systemctl", "restart", "smbd"], None),
    ]


def test_samba_password_syncs_when_enabled_user_password_changes(monkeypatch) -> None:
    """验证已开启 Samba 的用户在自助改密和管理员重置密码时都会同步 SMB 密码。"""
    calls: list[tuple[str, str, bool]] = []

    def fake_enable(account_name: str, password: str):
        """模拟开启 Samba，测试重点放在后续密码同步是否被触发。"""
        from app.services.samba_service import SambaAccountPlan

        return SambaAccountPlan(account_name, True, "enabled", "已启用", True, [], updated_at="now")

    def fake_set_password(account_name: str, password: str, enabled: bool):
        """记录 Samba 密码同步调用，避免执行真实 smbpasswd 命令。"""
        from app.services.samba_service import SambaAccountPlan

        calls.append((account_name, password, enabled))
        return SambaAccountPlan(account_name, enabled, "enabled", "已启用", True, [], updated_at="now")

    monkeypatch.setattr("app.services.auth_service.enable_samba_account", fake_enable)
    monkeypatch.setattr("app.services.auth_service.set_samba_password", fake_set_password)
    monkeypatch.setattr("app.services.user_service.set_samba_password", fake_set_password)

    client = make_client()
    admin_token = login_as_admin(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    user = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "samba-pass",
            "real_name": "Samba Password",
            "role": "student",
            "state": "enabled",
            "password": "student123",
        },
    ).json()["data"]
    student_token = login_user(client, "samba-pass", "student123")
    student_headers = {"Authorization": f"Bearer {student_token}"}
    client.post("/api/auth/samba/update", headers=student_headers, json={"enabled": True, "current_password": "student123"})

    change_response = client.post(
        "/api/auth/password/change",
        headers=student_headers,
        json={"current_password": "student123", "new_password": "student456"},
    )
    admin_token = login_as_admin(client)
    reset_response = client.post(
        "/api/users/password/reset",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": user["id"], "password": "student789"},
    )

    assert change_response.status_code == 200
    assert reset_response.status_code == 200
    assert calls == [
        ("samba-pass", "student456", True),
        ("samba-pass", "student789", True),
    ]


def test_last_admin_user_cannot_be_deleted() -> None:
    """验证用户删除流程保护最后一个管理员，避免系统失去管理入口。"""
    client = make_client()
    token = login_as_admin(client)
    response = client.delete("/api/users/1", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


def login_user(client: TestClient, identity: str, password: str) -> str:
    """使用指定账号登录，并返回后续测试可复用的 Bearer token。"""
    response = client.post(
        "/api/auth/login",
        json={"identity": identity, "password": password},
    )
    return response.json()["data"]["access_token"]


def test_file_manager_crud_uses_user_root_boundary(monkeypatch, tmp_path: Path) -> None:
    """验证文件管理接口在虚拟根目录 / 内完成新建、预览、保存、复制、重命名和删除。"""
    user_home_root = tmp_path / "user"
    monkeypatch.setenv("NEBULAGRID_USER_HOME_ROOT", str(user_home_root))
    monkeypatch.setenv("NEBULAGRID_VISIBLE_ROOTS", str(user_home_root))
    get_settings.cache_clear()
    try:
        client = make_client()
        admin_token = login_as_admin(client)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        client.post(
            "/api/users",
            headers=admin_headers,
            json={
                "username": "file-student",
                "real_name": "File Student",
                "role": "student",
                "state": "enabled",
                "password": "student123",
            },
        )
        user_root = user_home_root / "file-student"
        student_token = login_user(client, "file-student", "student123")
        headers = {"Authorization": f"Bearer {student_token}"}

        mkdir_response = client.post("/api/files/mkdir", headers=headers, json={"path": "/project"})
        create_response = client.post(
            "/api/files/create",
            headers=headers,
            json={"path": "/project/note.txt", "content": "hello"},
        )
        save_response = client.post(
            "/api/files/save",
            headers=headers,
            json={"path": "/project/note.txt", "content": "updated"},
        )
        copy_response = client.post(
            "/api/files/copy",
            headers=headers,
            json={"path": "/project/note.txt", "target_path": "/project/copy.txt"},
        )
        rename_response = client.post(
            "/api/files/rename",
            headers=headers,
            json={"path": "/project/copy.txt", "target_path": "/project/renamed.txt"},
        )
        preview_response = client.get("/api/files/preview?path=/project/renamed.txt", headers=headers)
        permission_response = client.post(
            "/api/files/permissions/execute",
            headers=headers,
            json={"path": "/project/renamed.txt"},
        )
        download_response = client.get("/api/files/download?path=/project/renamed.txt", headers=headers)
        delete_response = client.delete("/api/files?path=/project/renamed.txt", headers=headers)
        root_delete_response = client.delete("/api/files?path=/", headers=headers)

        assert mkdir_response.status_code == 200
        assert create_response.status_code == 200
        assert save_response.status_code == 200
        assert copy_response.status_code == 200
        assert rename_response.status_code == 200
        assert preview_response.json()["data"]["content"] == "updated"
        assert preview_response.json()["data"]["mode_octal"]
        assert permission_response.status_code == 200
        assert permission_response.json()["data"]["owner_executable"] is True
        assert (user_root / "project" / "renamed.txt").stat().st_mode & stat.S_IXUSR
        assert download_response.text == "updated"
        assert delete_response.status_code == 200
        assert root_delete_response.status_code == 422
        assert user_root.is_dir()
        assert (user_root / "project" / "note.txt").read_text(encoding="utf-8") == "updated"
        assert not (user_root / "project" / "renamed.txt").exists()
    finally:
        get_settings.cache_clear()


def test_shared_folder_scope_allows_copy_in_both_directions(monkeypatch, tmp_path: Path) -> None:
    """验证共享文件夹 scope 使用独立根目录，并允许用户在个人目录和共享目录之间复制。"""
    user_home_root = tmp_path / "user"
    shared_root = tmp_path / "shared"
    monkeypatch.setenv("NEBULAGRID_USER_HOME_ROOT", str(user_home_root))
    monkeypatch.setenv("NEBULAGRID_VISIBLE_ROOTS", str(user_home_root))
    monkeypatch.setenv("NEBULAGRID_SHARED_FOLDER_ROOT", str(shared_root))
    get_settings.cache_clear()
    try:
        client = make_client()
        admin_token = login_as_admin(client)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        client.post(
            "/api/users",
            headers=admin_headers,
            json={
                "username": "shared-student",
                "real_name": "Shared Student",
                "role": "student",
                "state": "enabled",
                "password": "student123",
            },
        )
        user_root = user_home_root / "shared-student"
        shared_root.mkdir(parents=True, exist_ok=True)
        (shared_root / "dataset.txt").write_text("shared-data", encoding="utf-8")

        student_token = login_user(client, "shared-student", "student123")
        headers = {"Authorization": f"Bearer {student_token}"}
        client.post("/api/files/mkdir", headers=headers, json={"path": "/project"})
        client.post(
            "/api/files/create",
            headers=headers,
            json={"path": "/project/result.txt", "content": "user-data"},
        )

        shared_list_response = client.get("/api/files/list?scope=shared&path=/", headers=headers)
        copy_to_shared_response = client.post(
            "/api/files/copy",
            headers=headers,
            json={
                "path": "/project/result.txt",
                "target_path": "/result.txt",
                "scope": "own",
                "target_scope": "shared",
            },
        )
        copy_to_own_response = client.post(
            "/api/files/copy",
            headers=headers,
            json={
                "path": "/dataset.txt",
                "target_path": "/project/dataset.txt",
                "scope": "shared",
                "target_scope": "own",
            },
        )
        preview_response = client.get("/api/files/preview?scope=shared&path=/dataset.txt", headers=headers)

        assert shared_list_response.status_code == 200
        assert shared_list_response.json()["data"]["display_path"] == "/共享文件夹"
        assert shared_list_response.json()["data"]["items"][0]["path"] == "/dataset.txt"
        assert copy_to_shared_response.status_code == 200
        assert copy_to_own_response.status_code == 200
        assert preview_response.json()["data"]["content"] == "shared-data"
        assert (shared_root / "result.txt").read_text(encoding="utf-8") == "user-data"
        assert (user_root / "project" / "dataset.txt").read_text(encoding="utf-8") == "shared-data"
    finally:
        get_settings.cache_clear()


def test_mentor_can_browse_assigned_student_files_readonly(monkeypatch, tmp_path: Path) -> None:
    """验证导师学生文件视图只展示名下学生，并通过只读 scope 访问学生 home。"""
    user_home_root = tmp_path / "user"
    monkeypatch.setenv("NEBULAGRID_USER_HOME_ROOT", str(user_home_root))
    monkeypatch.setenv("NEBULAGRID_VISIBLE_ROOTS", str(user_home_root))
    get_settings.cache_clear()
    try:
        client = make_client()
        admin_token = login_as_admin(client)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        mentor = client.post(
            "/api/users",
            headers=admin_headers,
            json={
                "username": "mentor-files",
                "real_name": "Mentor Files",
                "role": "mentor",
                "state": "enabled",
                "password": "mentor123",
            },
        ).json()["data"]
        assigned = client.post(
            "/api/users",
            headers=admin_headers,
            json={
                "username": "assigned-student",
                "real_name": "Assigned Student",
                "role": "student",
                "state": "enabled",
                "password": "student123",
                "supervisor_ids": [mentor["id"]],
            },
        ).json()["data"]
        client.post(
            "/api/users",
            headers=admin_headers,
            json={
                "username": "other-student",
                "real_name": "Other Student",
                "role": "student",
                "state": "enabled",
                "password": "student123",
            },
        )
        project_dir = user_home_root / assigned["username"] / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "note.txt").write_text("mentor-visible", encoding="utf-8")

        mentor_token = login_user(client, "mentor-files", "mentor123")
        headers = {"Authorization": f"Bearer {mentor_token}"}
        root_response = client.get("/api/files/list?scope=students&path=/", headers=headers)
        student_response = client.get("/api/files/list?scope=students&path=/assigned-student/project", headers=headers)
        preview_response = client.get("/api/files/preview?scope=students&path=/assigned-student/project/note.txt", headers=headers)
        forbidden_response = client.get("/api/files/list?scope=students&path=/other-student", headers=headers)

        root_names = [item["name"] for item in root_response.json()["data"]["items"]]
        assert root_response.status_code == 200
        assert root_names == ["Assigned Student"]
        assert student_response.status_code == 200
        assert student_response.json()["data"]["display_path"] == "/Assigned Student/project"
        assert student_response.json()["data"]["items"][0]["path"] == "/assigned-student/project/note.txt"
        assert preview_response.json()["data"]["content"] == "mentor-visible"
        assert forbidden_response.status_code == 403
    finally:
        get_settings.cache_clear()
