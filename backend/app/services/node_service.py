from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.errors import not_found, validation_error
from app.core.rbac import Role
from app.core.rbac import require_permission
from app.db.models import EnvInstallJob, Gpu, Node, TaskAllocation, TaskRequirement, TaskRuntimeGuard, User, UserSupervisor
from app.schemas.nodes import GpuInfo, NodeCreateRequest, NodeInfo, NodeSaveRequest, NodeUpdateRequest
from app.services.audit_service import record_audit
from app.services.auth_service import UserRecord
from app.services.metrics_service import LatestMetrics, get_latest_metrics


def list_nodes(user: UserRecord, db: Session, visible_only: bool = True) -> list[NodeInfo]:
    """返回数据库中的计算节点，并从 InfluxDB 附带最新监控快照。"""
    require_permission(user.role, "nodes:read")
    nodes = db.scalars(
        select(Node)
        .options(selectinload(Node.gpus))
        .order_by(Node.id)
    ).all()
    compute_nodes = [node for node in nodes if not is_control_plane_node(node)]
    if visible_only and user.role != Role.ADMIN:
        compute_nodes = [node for node in compute_nodes if can_user_access_node(user, node, db)]
    latest_metrics = load_latest_metrics(compute_nodes)
    occupied_gpu_ids = load_occupied_gpu_ids(compute_nodes, db)
    return [build_node_info(node, latest_metrics, occupied_gpu_ids) for node in compute_nodes]


def create_node(user: UserRecord, payload: NodeCreateRequest, db: Session) -> NodeInfo:
    """登记计算节点；GPU 可先手填，后续监控会按 nvidia-smi 结果自动校正。"""
    require_permission(user.role, "nodes:write")
    if is_control_plane_identity(payload.name, payload.ip):
        raise validation_error("master/control-plane node should not be registered as compute node")
    owner_ids = validate_owner_ids(payload.owner_user_ids, db)
    node = Node(
        name=payload.name.strip(),
        ip=payload.ip.strip(),
        ssh_user=payload.ssh_user.strip(),
        owner_type=payload.access_scope,
        owner_user_id=owner_ids[0] if owner_ids else None,
        owner_user_ids=owner_ids,
        access_scope=payload.access_scope,
        sharing_scope=payload.sharing_scope,
        is_public=payload.access_scope == "public",
        max_speed_mbps=payload.max_speed_mbps,
        state="offline",
        scheduling_enabled=False,
    )
    sync_node_gpus(node, payload)
    db.add(node)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise validation_error("node name already exists") from exc
    db.refresh(node)
    node_info = build_node_info(node, LatestMetrics())
    record_audit(user.id, "node.create", "node", str(node.id), detail_json=node_info.model_dump())
    return node_info


def update_node(user: UserRecord, node_id: int, payload: NodeUpdateRequest, db: Session) -> NodeInfo:
    """修改节点基础信息和 GPU 顺序清单，保留相同 index 的历史监控关联。"""
    require_permission(user.role, "nodes:write")
    node = require_node_model(node_id, db)
    if is_control_plane_identity(payload.name, payload.ip):
        raise validation_error("master/control-plane node should not be registered as compute node")
    owner_ids = validate_owner_ids(payload.owner_user_ids, db)
    node.name = payload.name.strip()
    node.ip = payload.ip.strip()
    node.ssh_user = payload.ssh_user.strip()
    node.owner_type = payload.access_scope
    node.owner_user_id = owner_ids[0] if owner_ids else None
    node.owner_user_ids = owner_ids
    node.access_scope = payload.access_scope
    node.sharing_scope = payload.sharing_scope
    node.is_public = payload.access_scope == "public"
    node.max_speed_mbps = payload.max_speed_mbps
    sync_node_gpus(node, payload)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise validation_error("node name already exists") from exc
    db.refresh(node)
    node_info = build_node_info(node, load_latest_metrics([node]), load_occupied_gpu_ids([node], db))
    record_audit(user.id, "node.update", "node", str(node.id), detail_json=node_info.model_dump())
    return node_info


def delete_node(user: UserRecord, node_id: int, db: Session) -> NodeInfo:
    """删除计算节点并清理直接外键引用，避免历史任务记录阻塞节点退役。"""
    require_permission(user.role, "nodes:write")
    node = require_node_model(node_id, db)
    node_info = build_node_info(node, load_latest_metrics([node]), load_occupied_gpu_ids([node], db))
    db.query(TaskRequirement).filter(TaskRequirement.node_id == node.id).update({TaskRequirement.node_id: None}, synchronize_session=False)
    db.query(EnvInstallJob).filter(EnvInstallJob.target_node_id == node.id).update({EnvInstallJob.target_node_id: None}, synchronize_session=False)
    db.query(TaskAllocation).filter(TaskAllocation.node_id == node.id).delete(synchronize_session=False)
    db.query(TaskRuntimeGuard).filter(TaskRuntimeGuard.node_id == node.id).delete(synchronize_session=False)
    db.delete(node)
    db.commit()
    record_audit(user.id, "node.delete", "node", str(node_info.id), detail_json=node_info.model_dump())
    return node_info


def get_node(node_id: int, db: Session) -> NodeInfo | None:
    """按 ID 查找节点，并隐藏 master/control-plane 节点。"""
    node = db.get(Node, node_id)
    if node is None or is_control_plane_node(node):
        return None
    return build_node_info(node, load_latest_metrics([node]), load_occupied_gpu_ids([node], db))


def reconnect_node(user: UserRecord, node_id: int, db: Session) -> NodeInfo:
    """把节点标记为等待监控器重连，下一轮 SSH 成功后会自动恢复在线。"""
    require_permission(user.role, "nodes:write")
    node = require_node_model(node_id, db)
    node.state = "reconnecting"
    db.commit()
    record_audit(user.id, "node.reconnect", "node", str(node.id))
    return build_node_info(node, load_latest_metrics([node]))


def force_offline_node(user: UserRecord, node_id: int, db: Session) -> NodeInfo:
    """强制节点离线并关闭调度开关，监控器不会自动打开手动下线节点。"""
    require_permission(user.role, "nodes:write")
    node = require_node_model(node_id, db)
    node.state = "manual_offline"
    node.scheduling_enabled = False
    db.commit()
    record_audit(user.id, "node.force_offline", "node", str(node.id))
    return build_node_info(node, load_latest_metrics([node]))


def require_node(node_id: int, db: Session) -> NodeInfo:
    """返回节点信息，找不到时抛出统一 NOT_FOUND 业务错误。"""
    node = get_node(node_id, db)
    if node is None:
        raise not_found("node not found")
    return node


def require_node_model(node_id: int, db: Session) -> Node:
    """返回 ORM 节点对象，供管理动作和 worker 共用隐藏 master 的规则。"""
    node = db.get(Node, node_id)
    if node is None or is_control_plane_node(node):
        raise not_found("node not found")
    return node


def build_node_info(
    node: Node,
    latest_metrics: LatestMetrics,
    occupied_gpu_ids: set[int] | None = None,
) -> NodeInfo:
    """把节点 ORM 对象转换为前端模型，并附带 InfluxDB 最新监控快照。"""
    metric = latest_metrics.nodes.get(node.id)
    metric_bandwidth = metric.network_bandwidth_mbps if metric else None
    occupied_gpu_ids = occupied_gpu_ids or set()
    return NodeInfo(
        id=node.id,
        name=node.name,
        ip=node.ip,
        ssh_user=node.ssh_user,
        owner_type=node.owner_type,
        owner_user_id=node.owner_user_id,
        owner_user_ids=normalize_owner_ids(node),
        access_scope=getattr(node, "access_scope", None) or ("public" if node.is_public else "private"),
        sharing_scope=getattr(node, "sharing_scope", None) or ("public" if node.is_public else "none"),
        is_public=node.is_public,
        max_speed_mbps=node.max_speed_mbps,
        state=node.state,
        scheduling_enabled=node.scheduling_enabled,
        gpus=[
            build_gpu_info(gpu, latest_metrics, occupied_gpu_ids)
            for gpu in sorted(node.gpus, key=lambda item: item.gpu_index)
        ],
        cpu_usage=metric.cpu_usage if metric else None,
        avail_ram_mb=metric.avail_ram_mb if metric else None,
        network_bandwidth_mbps=metric_bandwidth or node.max_speed_mbps,
        upload_mbps=metric.upload_mbps if metric else None,
        download_mbps=metric.download_mbps if metric else None,
        metric_collected_at=metric.collected_at if metric else None,
    )


def sync_node_gpus(node: Node, payload: NodeSaveRequest) -> None:
    """按 nvidia-smi 顺序同步 GPU 列表，同 index 更新可保留已有监控与调度引用。"""
    existing_by_index = {gpu.gpu_index: gpu for gpu in node.gpus}
    desired_indices = set(range(payload.gpu_count or 0))
    for index, model in enumerate(payload.gpu_models):
        gpu = existing_by_index.get(index)
        if gpu is None:
            node.gpus.append(Gpu(gpu_index=index, model=model, total_vram_mb=0))
        else:
            gpu.model = model
    for gpu in list(node.gpus):
        if gpu.gpu_index not in desired_indices:
            node.gpus.remove(gpu)


def validate_owner_ids(owner_ids: list[int], db: Session) -> list[int]:
    """校验节点所有人必须来自用户表，防止保存悬空 owner 导致共享判断错误。"""
    deduped: list[int] = []
    for owner_id in owner_ids:
        if owner_id not in deduped:
            deduped.append(owner_id)
    if not deduped:
        return []
    found = set(db.scalars(select(User.id).where(User.id.in_(deduped))).all())
    if found != set(deduped):
        raise validation_error("node owner not found")
    return deduped


def normalize_owner_ids(node: Node) -> list[int]:
    """兼容旧库里的单 owner 字段，统一返回多 owner 列表。"""
    owner_ids = list(getattr(node, "owner_user_ids", None) or [])
    if not owner_ids and node.owner_user_id is not None:
        owner_ids = [node.owner_user_id]
    deduped: list[int] = []
    for owner_id in owner_ids:
        if owner_id not in deduped:
            deduped.append(owner_id)
    return deduped


def can_user_access_node(user: UserRecord, node: Node, db: Session) -> bool:
    """按节点所有人与共享范围判断普通用户是否能在总览中看到并使用该节点。"""
    owner_ids = normalize_owner_ids(node)
    if user.id in owner_ids:
        return True
    sharing_scope = getattr(node, "sharing_scope", None) or ("public" if node.is_public else "none")
    if sharing_scope == "public":
        return True
    if sharing_scope != "group" or not owner_ids:
        return False
    return user.id in load_group_shared_user_ids(owner_ids, db)


def load_group_shared_user_ids(owner_ids: list[int], db: Session) -> set[int]:
    """展开组内共享对象：学生 owner 共享给其导师名下学生，导师 owner 共享给其学生。"""
    owners = db.scalars(select(User).where(User.id.in_(owner_ids))).all()
    shared_user_ids: set[int] = set()
    for owner in owners:
        if owner.role == Role.STUDENT.value:
            supervisor_ids = db.scalars(
                select(UserSupervisor.supervisor_id).where(UserSupervisor.student_id == owner.id)
            ).all()
            if supervisor_ids:
                shared_user_ids.update(
                    db.scalars(
                        select(UserSupervisor.student_id).where(UserSupervisor.supervisor_id.in_(supervisor_ids))
                    ).all()
                )
        elif owner.role == Role.MENTOR.value:
            shared_user_ids.update(
                db.scalars(
                    select(UserSupervisor.student_id).where(UserSupervisor.supervisor_id == owner.id)
                ).all()
            )
    return shared_user_ids


def build_gpu_info(gpu: Gpu, latest_metrics: LatestMetrics, occupied_gpu_ids: set[int] | None = None) -> GpuInfo:
    """把 GPU ORM 对象转换为前端模型，并附带 InfluxDB 最新监控快照。"""
    metric = latest_metrics.gpus.get(gpu.id)
    occupied_gpu_ids = occupied_gpu_ids or set()
    return GpuInfo(
        id=gpu.id,
        gpu_index=gpu.gpu_index,
        gpu_uuid=getattr(gpu, "gpu_uuid", "") or "",
        model=gpu.model,
        total_vram_mb=gpu.total_vram_mb,
        schedulable=gpu.schedulable,
        scheduled_occupied=gpu.id in occupied_gpu_ids,
        remark=gpu.remark,
        free_vram_mb=metric.free_vram_mb if metric else None,
        gpu_usage=metric.gpu_usage if metric else None,
        process_count=metric.process_count if metric else None,
        metric_collected_at=metric.collected_at if metric else None,
    )


def load_occupied_gpu_ids(nodes: list[Node], db: Session) -> set[int]:
    """Return GPU IDs currently held by unreleased scheduler allocations."""
    node_ids = [node.id for node in nodes]
    if not node_ids:
        return set()
    allocations = db.scalars(
        select(TaskAllocation)
        .where(TaskAllocation.node_id.in_(node_ids))
        .where(TaskAllocation.released_at.is_(None))
    ).all()
    occupied: set[int] = set()
    for allocation in allocations:
        occupied.update(coerce_int(gpu_id) for gpu_id in allocation.gpu_ids)
    return occupied


def load_latest_metrics(nodes: list[Node]) -> LatestMetrics:
    """读取节点和 GPU 的 InfluxDB 最新快照，InfluxDB 不可用时保持列表可用。"""
    node_ids = [node.id for node in nodes]
    gpu_ids = [gpu.id for node in nodes for gpu in node.gpus]
    try:
        return get_latest_metrics(node_ids, gpu_ids)
    except Exception:
        return LatestMetrics()


def is_control_plane_node(node: Node) -> bool:
    """识别 master/control-plane 节点，避免它们出现在计算节点列表中。"""
    return is_control_plane_identity(node.name, node.ip)


def is_control_plane_identity(name: str, ip: str) -> bool:
    """根据登记名和地址过滤控制节点；生产环境应只登记真实计算节点。"""
    lowered_name = name.strip().lower()
    lowered_ip = ip.strip().lower()
    return (
        "master" in lowered_name
        or "control" in lowered_name
        or lowered_ip in {"127.0.0.1", "localhost", "::1"}
    )
