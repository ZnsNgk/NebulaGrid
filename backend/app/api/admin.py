from fastapi import APIRouter, Body, Depends, Header, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.core.security import parse_authorization_header
from app.db.session import get_db
from app.schemas.admin import SettingsUpdateRequest
from app.schemas.auth import AdminLoginSessionOfflineRequest, AdminLoginSessionQuery
from app.schemas.nodes import NodeCreateRequest, NodeUpdateRequest
from app.services.audit_service import list_audit_logs, list_settings, update_settings
from app.services.auth_service import (
    UserRecord,
    list_admin_online_users,
    list_admin_user_login_sessions,
    offline_login_session_as_admin,
)
from app.services.node_service import create_node, delete_node, force_offline_node, reconnect_node, update_node

router = APIRouter()


@router.post("/nodes")
def post_admin_node(
    payload: NodeCreateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """管理员新增计算节点。"""
    node = create_node(current_user, payload, db)
    return api_success(data=node.model_dump(), request_id=request.headers.get("x-request-id"))


@router.put("/nodes/{node_id}")
def put_admin_node(
    node_id: int,
    payload: NodeUpdateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """管理员修改计算节点登记信息和 GPU 顺序清单。"""
    node = update_node(current_user, node_id, payload, db)
    return api_success(data=node.model_dump(), request_id=request.headers.get("x-request-id"))


@router.delete("/nodes/{node_id}")
def delete_admin_node(
    node_id: int,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """管理员删除计算节点，并清理调度引用。"""
    node = delete_node(current_user, node_id, db)
    return api_success(data=node.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/nodes/{node_id}/reconnect")
def post_admin_node_reconnect(
    node_id: int,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """管理员触发节点重连。"""
    node = reconnect_node(current_user, node_id, db)
    return api_success(data=node.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/nodes/{node_id}/force-offline")
def post_admin_node_force_offline(
    node_id: int,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """管理员强制节点下线并关闭调度。"""
    node = force_offline_node(current_user, node_id, db)
    return api_success(data=node.model_dump(), request_id=request.headers.get("x-request-id"))




@router.post("/login-management/online-users")
def post_admin_online_users(
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """登录管理：查看当前在线用户。"""
    users = [item.model_dump() for item in list_admin_online_users(current_user)]
    return api_success(data=users, request_id=request.headers.get("x-request-id"))


@router.post("/login-management/user-sessions")
def post_admin_user_login_sessions(
    request: Request,
    payload: AdminLoginSessionQuery | None = Body(default=None),
    authorization: str | None = Header(default=None),
    current_user: UserRecord = Depends(get_current_user),
):
    """登录管理：查看某个用户的上线设备、IP 和在线状态。"""
    payload = payload or AdminLoginSessionQuery()
    token = parse_authorization_header(authorization) if authorization else None
    items = [
        item.model_dump()
        for item in list_admin_user_login_sessions(
            current_user,
            user_id=payload.user_id,
            keyword=payload.keyword,
            current_token=token,
        )
    ]
    return api_success(data=items, request_id=request.headers.get("x-request-id"))


@router.post("/login-management/offline-session")
def post_admin_offline_login_session(
    payload: AdminLoginSessionOfflineRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    current_user: UserRecord = Depends(get_current_user),
):
    """登录管理：管理员手动下线任意用户的某个登录设备。"""
    token = parse_authorization_header(authorization) if authorization else None
    session = offline_login_session_as_admin(current_user, payload.session_id, token)
    return api_success(data=session.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/audit-logs")
def get_admin_audit_logs(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    category: str | None = Query(default=None, max_length=32),
    keyword: str | None = Query(default=None, max_length=128),
    action: str | None = Query(default=None, max_length=128),
    start_time: str | None = Query(default=None, max_length=40),
    end_time: str | None = Query(default=None, max_length=40),
    current_user: UserRecord = Depends(get_current_user),
):
    """分页查询审计日志。"""
    items, total = list_audit_logs(current_user, page, page_size, category, keyword, action, start_time, end_time)
    data = {
        "items": [item.model_dump() for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "category": category or "all",
        "filters": {
            "keyword": keyword or "",
            "action": action or "",
            "start_time": start_time or "",
            "end_time": end_time or "",
        },
    }
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
