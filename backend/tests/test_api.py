from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.time_utils import local_datetime
from app.services.auth_service import hash_session_token
from app.services.metrics_service import parse_flux_csv
from app.db.models import LoginSession, Node, Setting, Task, TaskAllocation, TaskRuntimeGuard
from app.db.session import SessionLocal
from app.main import create_app
from app.workers.node_monitor import monitor_node, node_monitor_paused, sync_gpu_inventory
from app.workers.runtime_guard import expand_pid_tree, parse_gpu_apps, parse_process_table
from app.workers.scheduler import scheduler_interval_seconds, scheduler_tick


def make_client() -> TestClient:
    """创建隔离的测试客户端，避免测试直接复用全局 app 状态。"""
    return TestClient(create_app())


def test_health_check_returns_ok() -> None:
    """验证健康检查接口使用统一响应格式并返回 ok 状态。"""
    client = make_client()
    response = client.get("/api/health")
    payload = response.json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["data"]["status"] == "ok"


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
    }
    node_id = client.post("/api/admin/nodes", headers=headers, json=node_payload).json()["data"]["id"]

    with SessionLocal() as db:
        node = db.get(Node, node_id)
        assert node is not None
        sync_gpu_inventory(
            db,
            node,
            [
                {"index": 0, "uuid": "GPU-visible", "name": "RTX 4090", "memory_total_mb": 24576},
                {"index": 1, "uuid": "GPU-display", "name": "NVIDIA T400", "memory_total_mb": 4096},
            ],
        )
        db.commit()

    nodes = client.get("/api/nodes", headers=headers).json()["data"]
    node = next(item for item in nodes if item["id"] == node_id)
    assert [gpu["model"] for gpu in node["gpus"]] == ["RTX 4090", "NVIDIA T400"]
    assert [gpu["schedulable"] for gpu in node["gpus"]] == [True, False]


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


def test_force_offline_node_cancels_running_tasks_and_releases_gpu(monkeypatch) -> None:
    """验证管理员强制下线节点会中止运行任务，并立即释放该节点 GPU 调度占用。"""
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

    ssh_commands: list[list[str]] = []

    def fake_check_output(command, text=True, stderr=None, timeout=None):
        """记录远端终止命令，避免测试环境真实 SSH 到计算节点。"""
        ssh_commands.append(list(command))
        return ""

    monkeypatch.setattr("app.services.node_service.subprocess.check_output", fake_check_output)

    offline_response = client.post(f"/api/admin/nodes/{node_id}/force-offline", headers=headers)

    with SessionLocal() as db:
        node = db.get(Node, node_id)
        task = db.scalar(select(Task).where(Task.task_id == task_id))
        assert task is not None
        allocation = db.scalar(select(TaskAllocation).where(TaskAllocation.task_id == task.id))
        guard = db.scalar(select(TaskRuntimeGuard).where(TaskRuntimeGuard.task_id == task.id))

    assert offline_response.status_code == 200
    assert offline_response.json()["data"]["state"] == "offline"
    assert offline_response.json()["data"]["gpus"][0]["scheduled_occupied"] is False
    assert node is not None
    assert node.state == "offline"
    assert node.scheduling_enabled is False
    assert task.state == "cancelled"
    assert task.last_block_reason == "节点被管理员强制下线"
    assert allocation is not None
    assert allocation.released_at is not None
    assert guard is not None
    assert guard.state == "cancelled"
    assert ssh_commands
    assert "kill -KILL -4321" in ssh_commands[0][-1]


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
        download_response = client.get("/api/files/download?path=/project/renamed.txt", headers=headers)
        delete_response = client.delete("/api/files?path=/project/renamed.txt", headers=headers)
        root_delete_response = client.delete("/api/files?path=/", headers=headers)

        assert mkdir_response.status_code == 200
        assert create_response.status_code == 200
        assert save_response.status_code == 200
        assert copy_response.status_code == 200
        assert rename_response.status_code == 200
        assert preview_response.json()["data"]["content"] == "updated"
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
