from itertools import count

from app.core.rbac import require_permission
from app.schemas.nodes import GpuInfo, NodeCreateRequest, NodeInfo
from app.services.audit_service import record_audit
from app.services.auth_service import UserRecord

_NODE_ID = count(2)
_GPU_ID = count(3)
_NODES: list[NodeInfo] = [
    NodeInfo(
        id=1,
        name="master-demo",
        ip="127.0.0.1",
        ssh_user="ddltm",
        state="online",
        scheduling_enabled=True,
        gpus=[
            GpuInfo(id=1, gpu_index=0, model="Demo GPU", total_vram_mb=24576),
            GpuInfo(id=2, gpu_index=1, model="Demo GPU", total_vram_mb=24576),
        ],
    )
]


def list_nodes(user: UserRecord) -> list[NodeInfo]:
    """返回用户可见节点列表，展示者和登录用户都只能通过服务层获取数据。"""
    require_permission(user.role, "nodes:read")
    return _NODES


def create_node(user: UserRecord, payload: NodeCreateRequest) -> NodeInfo:
    """创建新计算节点，并把初始 GPU 型号展开为 GPU 子资源。"""
    require_permission(user.role, "nodes:write")
    node_id = next(_NODE_ID)
    gpus = [
        GpuInfo(id=next(_GPU_ID), gpu_index=index, model=model, total_vram_mb=0)
        for index, model in enumerate(payload.gpu_models)
    ]
    node = NodeInfo(
        id=node_id,
        name=payload.name,
        ip=payload.ip,
        ssh_user=payload.ssh_user,
        is_public=payload.is_public,
        max_speed_mbps=payload.max_speed_mbps,
        state="offline",
        scheduling_enabled=False,
        gpus=gpus,
    )
    _NODES.append(node)
    record_audit(user.id, "node.create", "node", str(node.id), detail_json=node.model_dump())
    return node


def get_node(node_id: int) -> NodeInfo | None:
    """按 ID 查找节点，供节点管理动作复用同一查找逻辑。"""
    return next((node for node in _NODES if node.id == node_id), None)


def reconnect_node(user: UserRecord, node_id: int) -> NodeInfo:
    """把节点标记为重连中，真实 SSH 重连会在后续 worker 中实现。"""
    require_permission(user.role, "nodes:write")
    node = require_node(node_id)
    node.state = "reconnecting"
    record_audit(user.id, "node.reconnect", "node", str(node.id))
    return node


def force_offline_node(user: UserRecord, node_id: int) -> NodeInfo:
    """强制节点离线并关闭调度开关，避免调度器继续选择该节点。"""
    require_permission(user.role, "nodes:write")
    node = require_node(node_id)
    node.state = "manual_offline"
    node.scheduling_enabled = False
    record_audit(user.id, "node.force_offline", "node", str(node.id))
    return node


def require_node(node_id: int) -> NodeInfo:
    """返回节点对象，找不到时抛出统一 NOT_FOUND 业务错误。"""
    from app.core.errors import not_found

    node = get_node(node_id)
    if node is None:
        raise not_found("node not found")
    return node

