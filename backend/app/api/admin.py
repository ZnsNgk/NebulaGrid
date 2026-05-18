from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.admin import SettingsUpdateRequest
from app.schemas.nodes import NodeCreateRequest
from app.services.audit_service import list_audit_logs, list_settings, update_settings
from app.services.auth_service import UserRecord
from app.services.node_service import create_node, force_offline_node, reconnect_node

router = APIRouter()


@router.post("/nodes")
def post_admin_node(
    payload: NodeCreateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """管理员新增计算节点。"""
    node = create_node(current_user, payload)
    return api_success(data=node.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/nodes/{node_id}/reconnect")
def post_admin_node_reconnect(
    node_id: int,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """管理员触发节点重连。"""
    node = reconnect_node(current_user, node_id)
    return api_success(data=node.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/nodes/{node_id}/force-offline")
def post_admin_node_force_offline(
    node_id: int,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """管理员强制节点下线并关闭调度。"""
    node = force_offline_node(current_user, node_id)
    return api_success(data=node.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/audit-logs")
def get_admin_audit_logs(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    current_user: UserRecord = Depends(get_current_user),
):
    """分页查询审计日志。"""
    items, total = list_audit_logs(current_user, page, page_size)
    data = {"items": [item.model_dump() for item in items], "total": total, "page": page, "page_size": page_size}
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.get("/settings")
def get_admin_settings(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """读取系统配置项。"""
    settings = [item.model_dump() for item in list_settings(current_user)]
    return api_success(data=settings, request_id=request.headers.get("x-request-id"))


@router.patch("/settings")
def patch_admin_settings(
    payload: SettingsUpdateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """更新系统配置项并写入审计日志。"""
    settings = [item.model_dump() for item in update_settings(current_user, payload.values)]
    return api_success(data=settings, request_id=request.headers.get("x-request-id"))

