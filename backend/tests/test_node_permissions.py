"""用隔离 SQLite 验证节点共享边界，避免访问真实集群、账号或监控服务。"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.base import Base
from app.db.models import Gpu, Node, User, UserSupervisor
from app.schemas.tasks import TaskRequirement
from app.services import node_service
from app.services.auth_service import user_model_to_record
from app.services.metrics_service import LatestMetrics
from app.services.task_service import validate_requirement_node
from app.workers.scheduler import node_schedule_priority, select_gpu_allocation, visible_schedulable_nodes


@pytest.fixture
def group_node(monkeypatch):
    """包含双导师、各组同学、无导师学生及组外用户；仅替换外部监控读取。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(node_service, "load_latest_metrics", lambda nodes: LatestMetrics())
    with Session(engine) as db:
        roles = {"mentor": "mentor", "second_mentor": "mentor", "outsider_mentor": "mentor",
                 "owner": "student", "peer": "student", "second_peer": "student",
                 "outsider": "student", "unassigned": "student"}
        users = {name: User(username=name, real_name=name, role=role,
                            password_hash="unused", home_path=f"/isolated/{name}")
                 for name, role in roles.items()}
        db.add_all(users.values())
        db.flush()
        for student, mentor in [("owner", "mentor"), ("owner", "second_mentor"),
                                ("peer", "mentor"), ("second_peer", "second_mentor"),
                                ("outsider", "outsider_mentor")]:
            db.add(UserSupervisor(student_id=users[student].id, supervisor_id=users[mentor].id))
        node = Node(name="student-private", ip="192.0.2.10", ssh_user="unused",
                    owner_user_id=users["owner"].id, owner_user_ids=[users["owner"].id],
                    access_scope="private", sharing_scope="group", is_public=False,
                    state="online", scheduling_enabled=True)
        node.gpus = [Gpu(gpu_index=0, model="Test GPU", total_vram_mb=24576, schedulable=True)]
        db.add(node)
        db.commit()
        yield db, users, node
    engine.dispose()


@pytest.mark.parametrize("entrypoint", ["list", "task", "scheduler"])
@pytest.mark.parametrize("name,allowed", [
    ("owner", True), ("mentor", True), ("second_mentor", True), ("peer", True),
    ("second_peer", True), ("outsider_mentor", False), ("outsider", False), ("unassigned", False),
])
def test_student_group_sharing_across_entrypoints(group_node, entrypoint, name, allowed):
    """分别验证展示、指定节点校验与自动/指定调度，防止只修好其中一条链路。"""
    db, users, node = group_node
    user = user_model_to_record(users[name], db)
    requirement = TaskRequirement(node_id=node.id, need_gpus=1)
    if entrypoint == "list":
        assert [item.id for item in node_service.list_nodes(user, db)] == ([node.id] if allowed else [])
    elif entrypoint == "task":
        if allowed:
            validate_requirement_node(user, requirement, db)
        else:
            with pytest.raises(AppError) as error:
                validate_requirement_node(user, requirement, db)
            assert error.value.status_code == 403
    else:
        for requested_node_id in (None, node.id):
            candidates = visible_schedulable_nodes(db, user, requested_node_id)
            assert candidates == ([node] if allowed else [])
            task = SimpleNamespace(requirement=requirement, env_id=None)
            allocation = select_gpu_allocation(db, candidates, task, LatestMetrics())
            if allowed:
                assert allocation == (node, node.gpus, "exclusive")
                assert node_schedule_priority(db, user, node) == (0 if name == "owner" else 1)
            else:
                assert allocation is None


@pytest.mark.parametrize("scope", ["none", "public"])
def test_other_sharing_scopes_keep_their_boundaries(group_node, scope):
    """组内授权修复不得让不共享节点被导师访问，也不得缩小公开共享范围。"""
    db, users, node = group_node
    node.sharing_scope = scope
    for name, model in users.items():
        assert node_service.can_user_access_node(user_model_to_record(model, db), node, db) == (
            scope == "public" or name == "owner"
        )


@pytest.mark.parametrize("owners,expected", [
    (["mentor"], {"mentor", "owner", "peer"}),
    (["unassigned"], {"unassigned"}),
    (["owner", "outsider"], {"owner", "mentor", "second_mentor", "peer", "second_peer",
                             "outsider", "outsider_mentor"}),
    ([], set()),
])
def test_group_sharing_owner_boundaries(group_node, owners, expected):
    """导师所有权不递归扩散到共同指导的其他组；多所有人取并集，无关系不扩大授权。"""
    db, users, node = group_node
    node.owner_user_ids = [users[name].id for name in owners]
    node.owner_user_id = node.owner_user_ids[0] if owners else None
    actual = {name for name, model in users.items()
              if node_service.can_user_access_node(user_model_to_record(model, db), node, db)}
    assert actual == expected


def test_legacy_owner_and_supervisor_changes(group_node):
    """兼容旧单所有人字段，并在解除导师关系后立即撤销该导师及其其他学生的访问。"""
    db, users, node = group_node
    node.owner_user_ids = []
    mentor = user_model_to_record(users["mentor"], db)
    assert node_service.can_user_access_node(mentor, node, db)
    db.execute(delete(UserSupervisor).where(UserSupervisor.student_id == users["owner"].id,
                                            UserSupervisor.supervisor_id == mentor.id))
    db.commit()
    for name in ("mentor", "peer"):
        assert not node_service.can_user_access_node(user_model_to_record(users[name], db), node, db)
    for name in ("owner", "second_mentor", "second_peer"):
        assert node_service.can_user_access_node(user_model_to_record(users[name], db), node, db)
