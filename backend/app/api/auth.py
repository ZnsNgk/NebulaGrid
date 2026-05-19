from fastapi import APIRouter, Depends, Header, Request

from app.api.deps import get_current_user
from app.core.security import parse_authorization_header
from app.core.responses import api_success
from app.schemas.auth import AccountUpdateRequest, LoginRequest, PasswordChangeRequest, SessionOfflineRequest
from app.services.audit_service import record_audit
from app.services.auth_service import (
    authenticate_user,
    build_public_user,
    change_current_user_password,
    list_login_sessions,
    logout_session,
    revoke_login_session,
    update_current_user_profile,
    UserRecord,
)

router = APIRouter()


@router.post("/login")
def login(payload: LoginRequest, request: Request):
    """校验账号密码并签发临时令牌，MVP 阶段先使用内存用户便于前端联调。"""
    login_result = authenticate_user(
        payload.identity,
        payload.password,
        get_request_ip(request),
        request.headers.get("user-agent", ""),
        get_request_device_id(request),
    )
    return api_success(data=login_result.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/logout")
def logout(request: Request, authorization: str | None = Header(default=None)):
    """退出当前登录设备。"""
    if authorization:
        logout_session(parse_authorization_header(authorization))
    return api_success(data={"logged_out": True}, request_id=request.headers.get("x-request-id"))


@router.post("/me")
def me(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """根据 Authorization 令牌返回当前用户资料和权限列表。"""
    return api_success(data=build_public_user(current_user).model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/me")
def get_me(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """兼容文档和旧测试中的 GET /auth/me，返回内容与 POST /auth/me 保持一致。"""
    return me(request, current_user)


@router.post("/me/update")
def post_me_update(
    payload: AccountUpdateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """允许用户更新自己的资料字段，用户名、角色和状态仍由管理端维护。"""
    user = update_current_user_profile(current_user, payload.real_name)
    record_audit(user.id, "user.profile.update", "user", str(user.id), ip=get_request_ip(request))
    return api_success(data=build_public_user(user).model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/password/change")
def post_password_change(
    payload: PasswordChangeRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """允许用户重设自己的登录密码，必须提供当前密码。"""
    change_current_user_password(current_user, payload.current_password, payload.new_password)
    record_audit(current_user.id, "user.password.change", "user", str(current_user.id), ip=get_request_ip(request))
    return api_success(data={"password_changed": True}, request_id=request.headers.get("x-request-id"))


@router.post("/change-password")
def post_change_password_alias(
    payload: PasswordChangeRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """兼容早期文档中的 /auth/change-password 路径。"""
    return post_password_change(payload, request, current_user)


@router.post("/sessions/list")
def post_sessions_list(
    request: Request,
    authorization: str | None = Header(default=None),
    current_user: UserRecord = Depends(get_current_user),
):
    """列出当前用户的登录 IP、设备和在线状态。"""
    token = parse_authorization_header(authorization)
    sessions = [item.model_dump() for item in list_login_sessions(current_user, token)]
    return api_success(data=sessions, request_id=request.headers.get("x-request-id"))


@router.post("/sessions/offline")
def post_session_offline(
    payload: SessionOfflineRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    current_user: UserRecord = Depends(get_current_user),
):
    """手动下线当前用户指定登录设备，会话归属由服务层校验。"""
    token = parse_authorization_header(authorization)
    session = revoke_login_session(current_user, payload.session_id, token)
    record_audit(current_user.id, "auth.session.offline", "login_session", str(payload.session_id), ip=get_request_ip(request))
    return api_success(data=session.model_dump(), request_id=request.headers.get("x-request-id"))


def get_request_ip(request: Request) -> str:
    """优先读取反向代理传入的真实 IP，缺失时退回连接来源。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def get_request_device_id(request: Request) -> str:
    """读取前端生成并持久化的设备 ID，用于区分同 IP 下的多台设备。"""
    return request.headers.get("x-ng-device-id", "")
