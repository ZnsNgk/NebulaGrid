"""隔离数据库验证用量、自然日裁剪和角色边界，不连接真实集群或监控库。"""

from datetime import timedelta
from urllib.error import HTTPError, URLError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.usage import router
from app.core.config import Settings
from app.core.errors import AppError
from app.core.time_utils import local_datetime
from app.db.base import Base
from app.db.models import Node, Task, TaskAllocation, TaskRequirement, User, UserSupervisor
from app.db.session import get_db
from app.services import metrics_service, usage_service
from app.services.auth_service import user_model_to_record


@pytest.fixture
def usage_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    now = local_datetime().replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(usage_service, "local_datetime", lambda: now)
    monkeypatch.setattr(usage_service, "get_daily_usage_metrics", lambda *args: ({}, "not_configured"))
    with Session(engine) as db:
        users = {}
        for name, role in [("student", "student"), ("peer", "student"), ("outsider", "student"),
                           ("mentor", "mentor"), ("admin", "admin"), ("viewer", "viewer")]:
            users[name] = User(username=name, real_name=name, role=role, password_hash="unused", home_path="/unused")
        db.add_all(users.values())
        db.flush()
        db.add_all([UserSupervisor(student_id=users[name].id, supervisor_id=users["mentor"].id)
                    for name in ("student", "peer")])
        nodes = {}
        for name, scope in [("public", "public"), ("private", "none"), ("group", "group"), ("master", "public")]:
            nodes[name] = Node(name=name, ip="192.0.2.1", ssh_user="unused", sharing_scope=scope,
                               owner_user_id=users["student"].id, owner_user_ids=[users["student"].id])
        db.add_all(nodes.values())
        db.commit()
        yield db, users, nodes, now
    engine.dispose()


def record(users, db, name="student"):
    return user_model_to_record(users[name], db)


def add_task(db, user, now, state="succeeded", start=None, finish=None, node=None, gpu_ids=None,
             allocated=None, released=None, created=None, need_gpus=1):
    """只构造业务统计字段；CPU 和未分配任务可单独覆盖需求数量。"""
    task = Task(task_id=f"usage-{len(db.identity_map)}-{db.query(Task).count()}", user_id=user.id,
                command="unused", workdir="/unused", state=state, started_at=start,
                finished_at=finish, created_at=created or now)
    task.requirement = TaskRequirement(need_gpus=need_gpus)
    db.add(task)
    db.flush()
    if node:
        db.add(TaskAllocation(task_id=task.id, node_id=node.id, gpu_ids=gpu_ids or [],
                              allocated_at=allocated or start or now, released_at=released))
    db.flush()
    return task


def test_task_totals_gpu_hours_cpu_running_and_missing(usage_db):
    db, users, nodes, now = usage_db
    add_task(db, users["student"], now, start=now - timedelta(hours=4), finish=now - timedelta(hours=1),
             node=nodes["public"], gpu_ids=[1, 2], released=now - timedelta(hours=1))
    add_task(db, users["student"], now, state="failed", start=now - timedelta(hours=2), finish=now, need_gpus=0)
    add_task(db, users["student"], now, state="running", start=now - timedelta(hours=1), node=nodes["public"], gpu_ids=[3])
    add_task(db, users["student"], now, state="cancelled", finish=now)
    add_task(db, users["student"], now, state="succeeded")
    result = usage_service.build_task_usage(record(users, db), db)
    own = result["personal"]
    assert own["submitted"] == 5 and own["executed"] == 3
    assert own["succeeded"] == 2 and own["failed"] == 1 and own["cancelled"] == 1
    assert own["runtime_seconds"] == 6 * 3600
    assert own["gpu_hours"] == 7
    assert own["unknown_duration_tasks"] == 1
    assert result["users"] == []


def test_all_records_and_roles_revoke_immediately(usage_db):
    db, users, _, now = usage_db
    for _ in range(205):
        add_task(db, users["student"], now, state="wait")
    add_task(db, users["outsider"], now, state="wait")
    mentor = record(users, db, "mentor")
    result = usage_service.build_task_usage(mentor, db)
    assert result["summary"]["submitted"] == 205
    assert {item["username"] for item in result["users"]} == {"student", "peer", "mentor"}
    assert result["personal"]["submitted"] == 0
    assert usage_service.build_task_usage(record(users, db, "admin"), db)["summary"]["submitted"] == 206
    db.execute(delete(UserSupervisor))
    assert usage_service.build_task_usage(mentor, db)["summary"]["submitted"] == 0
    with pytest.raises(AppError) as error:
        usage_service.build_task_usage(record(users, db, "viewer"), db)
    assert error.value.status_code == 403
    with pytest.raises(AppError):
        usage_service.build_node_usage(record(users, db, "viewer"), db)


def test_daily_union_window_and_personal_occupancy(usage_db):
    db, users, nodes, now = usage_db
    midnight = now.replace(hour=0)
    for owner, begin, end in [("student", midnight - timedelta(hours=1), midnight + timedelta(hours=2)),
                              ("student", midnight + timedelta(hours=1), midnight + timedelta(hours=3)),
                              ("outsider", midnight + timedelta(hours=2), midnight + timedelta(hours=4))]:
        add_task(db, users[owner], now, node=nodes["public"], allocated=begin, released=end)
    # 同一个用户在另一服务器占用一小时，需要另行累加。
    add_task(db, users["student"], now, node=nodes["private"], allocated=midnight, released=midnight + timedelta(hours=1))
    result = usage_service.build_node_usage(record(users, db), db)
    public = next(node for node in result["nodes"] if node["name"] == "public")
    assert len(public["daily"]) == 7
    assert public["occupied_seconds"] == 5 * 3600
    assert public["average_daily_seconds"] == pytest.approx(5 * 3600 / 7)
    assert public["daily"][-2]["occupied_seconds"] == 3600
    assert public["daily"][-1]["occupied_seconds"] == 4 * 3600
    assert public["daily"][-1]["own_occupied_seconds"] == 3 * 3600
    assert public["daily"][-1]["occupancy_percent"] == pytest.approx(100 / 3)
    assert result["own_occupied_seconds"] == 5 * 3600
    assert public["gpu_usage_percent"] is None
    assert public["daily"][0]["occupied_seconds"] == 0
    assert result["users"] == [] and "outsider" not in str(result)


@pytest.mark.parametrize("days", [7, 30, 90])
def test_window_clips_old_and_open_allocations_and_counts_creation(usage_db, days):
    db, users, nodes, now = usage_db
    add_task(db, users["student"], now, state="running", node=nodes["public"],
             allocated=now - timedelta(days=100), created=now - timedelta(days=100), gpu_ids=[1, 2])
    result = usage_service.build_node_usage(record(users, db, "mentor"), db, days)
    public = next(node for node in result["nodes"] if node["name"] == "public")
    assert public["occupied_seconds"] == ((days - 1) * 24 + 12) * 3600
    assert public["occupancy_percent"] == 100
    own = next(item for item in result["users"] if item["username"] == "student")
    assert own["submitted"] == 0
    assert own["occupied_seconds"] == public["occupied_seconds"]
    assert own["gpu_hours"] == ((days - 1) * 24 + 12) * 2
    assert public["users"][0]["gpu_hours"] == own["gpu_hours"]


def test_node_permissions_and_zero_users(usage_db):
    db, users, _, _ = usage_db
    for role, names in [("student", {"public", "private", "group"}), ("mentor", {"public", "group"}),
                        ("outsider", {"public"}), ("admin", {"public", "private", "group"})]:
        result = usage_service.build_node_usage(record(users, db, role), db)
        assert {node["name"] for node in result["nodes"]} == names
        if role in {"mentor", "admin"}:
            assert all(item["occupied_seconds"] == 0 for item in result["users"])
            assert all(item["gpu_hours"] == 0 for item in result["users"])
        assert all(node["users"] == [] for node in result["nodes"])


def test_node_user_card_hours_union_boundaries_and_permissions(usage_db):
    """本期卡时按实际 GPU 区间并集计算；跨节点累计且导师不能看到其他用户或私有节点用量。"""
    db, users, nodes, now = usage_db
    start = now.replace(hour=0) - timedelta(days=6)
    def allocation(owner, node, begin, end, cards):
        return add_task(db, users[owner], now, node=nodes[node], allocated=begin, released=end, gpu_ids=cards)
    allocation("student", "public", start - timedelta(hours=1), start + timedelta(hours=2), [1, 2, 2])
    allocation("student", "public", start + timedelta(hours=1), start + timedelta(hours=3), [2, 3])
    allocation("student", "public", now - timedelta(hours=1), None, [1])
    allocation("student", "private", now - timedelta(hours=2), now - timedelta(hours=1), [1, 2])
    allocation("peer", "public", now - timedelta(hours=2), now - timedelta(hours=1), [])
    allocation("outsider", "public", now - timedelta(hours=1), now, [1])
    # 零长度、完全位于周期外和异常反向区间都不应产生用量行或卡时。
    allocation("mentor", "public", now - timedelta(hours=1), now - timedelta(hours=1), [1])
    allocation("mentor", "public", start - timedelta(hours=2), start, [1])
    allocation("mentor", "public", now + timedelta(hours=1), None, [1])
    allocation("mentor", "public", now - timedelta(hours=1), now - timedelta(hours=2), [1])
    mentor_record = record(users, db, "mentor")
    mentor = usage_service.build_node_usage(mentor_record, db)
    public = next(node for node in mentor["nodes"] if node["name"] == "public")
    assert [(item["username"], item["occupied_seconds"], item["gpu_hours"]) for item in public["users"]] == [
        ("student", 4 * 3600, 8), ("peer", 3600, 0)]
    totals = {item["username"]: item for item in mentor["users"]}
    assert totals["student"]["gpu_hours"] == 8
    assert totals["mentor"]["gpu_hours"] == 0
    assert "outsider" not in str(mentor)
    admin = usage_service.build_node_usage(record(users, db, "admin"), db)
    totals = {item["username"]: item for item in admin["users"]}
    assert totals["student"]["occupied_seconds"] == 5 * 3600
    assert totals["student"]["gpu_hours"] == 10
    admin_public = next(node for node in admin["nodes"] if node["name"] == "public")
    assert {item["username"] for item in admin_public["users"]} == {"student", "peer", "outsider"}
    assert next(item for item in admin_public["users"] if item["username"] == "outsider")["gpu_hours"] == 1
    assert next(node for node in admin["nodes"] if node["name"] == "group")["users"] == []
    student = usage_service.build_node_usage(record(users, db), db)
    assert student["users"] == [] and all(node["users"] == [] for node in student["nodes"])
    db.execute(delete(UserSupervisor))
    revoked = usage_service.build_node_usage(mentor_record, db)
    assert all(node["users"] == [] for node in revoked["nodes"])
    assert {item["username"] for item in revoked["users"]} == {"mentor"}


def test_period_mean_weights_hours_and_keeps_occupancy_live(usage_db, monkeypatch):
    """缺一小时的日期按 23 小时加权；CPU/GPU 截至整点，任务占用仍累计到请求时刻。"""
    db, users, nodes, base_now = usage_db
    now = base_now + timedelta(minutes=25)
    midnight = now.replace(hour=0, minute=0)
    monkeypatch.setattr(usage_service, "local_datetime", lambda: now)
    metrics = {
        (nodes["public"].id, (midnight - timedelta(days=1)).date().isoformat()):
            {"hours": 23, "cpu_usage": 60, "gpu_usage": 30},
        (nodes["public"].id, midnight.date().isoformat()):
            {"hours": 12, "cpu_usage": 0, "gpu_usage": 0},
    }
    monkeypatch.setattr(usage_service, "get_daily_usage_metrics", lambda *args: (metrics, "building"))
    add_task(db, users["student"], now, state="running", node=nodes["public"], allocated=midnight)
    result = usage_service.build_node_usage(record(users, db), db)
    node = next(item for item in result["nodes"] if item["name"] == "public")
    assert node["cpu_usage_percent"] == pytest.approx(60 * 23 / 35)
    assert node["gpu_usage_percent"] == pytest.approx(30 * 23 / 35)
    assert node["daily"][-2]["metric_hours"] == 23
    assert node["daily"][-2]["expected_metric_hours"] == 24
    assert result["metrics_end_at"] == base_now.isoformat()
    assert node["daily"][-1]["expected_metric_hours"] == 12
    assert node["daily"][-1]["occupied_seconds"] == 12 * 3600 + 25 * 60


def test_unknown_duration_and_old_allocation_not_used_for_rerun(usage_db):
    db, users, nodes, now = usage_db
    add_task(db, users["student"], now, start=now - timedelta(hours=1), finish=now,
             node=nodes["public"], allocated=now - timedelta(days=2), released=now - timedelta(days=1), gpu_ids=[1, 2])
    add_task(db, users["student"], now, start=now, finish=now - timedelta(hours=1))
    totals = usage_service.build_task_usage(record(users, db), db)["personal"]
    assert totals["runtime_seconds"] == 3600
    assert totals["gpu_hours"] == 0
    assert totals["unknown_gpu_tasks"] == totals["unknown_duration_tasks"] == 1


def test_usage_routes_and_range_validation(usage_db):
    db, users, _, _ = usage_db
    app = FastAPI()
    app.include_router(router, prefix="/usage")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: record(users, db)
    with TestClient(app) as client:
        assert client.get("/usage/tasks").status_code == 200
        for days in (7, 30, 90):
            response = client.get(f"/usage/nodes?days={days}")
            assert response.status_code == 200
            assert response.json()["data"]["days"] == days
        for days in (0, 8, 365, "invalid"):
            assert client.get(f"/usage/nodes?days={days}").status_code == 422


@pytest.mark.parametrize("error,status", [
    (TimeoutError("read timed out"), "timeout"),
    (URLError(TimeoutError("connect timed out")), "timeout"),
    (HTTPError("http://influx.test", 401, "unauthorized", {}, None), "access_denied"),
    (HTTPError("http://influx.test", 403, "forbidden", {}, None), "access_denied"),
    (HTTPError("http://influx.test", 404, "missing bucket", {}, None), "building"),
    (HTTPError("http://influx.test", 400, "bad query", {}, None), "query_error"),
    (URLError("connection refused"), "unavailable"),
])
def test_history_failure_reason_is_not_reported_as_missing_data(monkeypatch, error, status):
    """真实网络异常路径不能一律提示服务不可用，更不能把失败误当作没有采样。"""
    now = local_datetime()
    monkeypatch.setattr(metrics_service, "get_settings", lambda: Settings(influxdb_token="test"))
    def fail(request, timeout):
        assert timeout == 5
        raise error
    monkeypatch.setattr(metrics_service.urllib.request, "urlopen", fail)
    assert metrics_service.get_daily_usage_metrics([1], now - timedelta(days=7), now) == ({}, status)


@pytest.mark.parametrize("configured,expected", [(None, 60), ("90", 90), ("1", 5), ("999", 120)])
def test_history_timeout_environment_is_bounded(monkeypatch, configured, expected):
    """运维可独立延长历史查询，但不能无上限占用后台汇总进程。"""
    from app.core.config import get_settings
    key = "NEBULAGRID_INFLUXDB_USAGE_TIMEOUT_SECONDS"
    if configured is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, configured)
    assert get_settings.__wrapped__().influxdb_usage_timeout_seconds == expected
