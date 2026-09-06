from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.db.session import get_db
from app.services.auth_service import UserRecord
from app.services.node_service import list_nodes
from app.services.dashboard_service import annotate_gpu_occupancy

router = APIRouter()


@router.get("")
def get_nodes(
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
    include_occupancy: bool = False,
):
    """返回当前用户可见的计算节点列表。"""
    node_infos = list_nodes(current_user, db)
    # 只有总览显式请求派生占用信息，其他节点列表不承担额外聚合开销。
    if include_occupancy:
        annotate_gpu_occupancy(node_infos, db)
    nodes = [node.model_dump() for node in node_infos]
    return api_success(data=nodes, request_id=request.headers.get("x-request-id"))

