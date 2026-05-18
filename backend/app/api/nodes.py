from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.db.session import get_db
from app.services.auth_service import UserRecord
from app.services.node_service import list_nodes

router = APIRouter()


@router.get("")
def get_nodes(
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回当前用户可见的计算节点列表。"""
    nodes = [node.model_dump() for node in list_nodes(current_user, db)]
    return api_success(data=nodes, request_id=request.headers.get("x-request-id"))

