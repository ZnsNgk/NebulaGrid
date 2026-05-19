from fastapi import APIRouter, Body, Depends, Request

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.users import (
    UserCreateRequest,
    UserDeleteRequest,
    UserListRequest,
    UserPasswordResetRequest,
    UserUpdateRequest,
)
from app.services.auth_service import UserRecord
from app.services.user_service import create_user, delete_user, list_users, reset_user_password, update_user

router = APIRouter()


@router.get("")
def get_users_list(
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """兼容早期 GET /users 调用；复杂筛选仍建议使用 POST /users/list。"""
    users = [user.model_dump() for user in list_users(current_user, UserListRequest())]
    return api_success(data=users, request_id=request.headers.get("x-request-id"))


@router.post("/list")
def post_users_list(
    request: Request,
    payload: UserListRequest | None = Body(default=None),
    current_user: UserRecord = Depends(get_current_user),
):
    """返回用户管理列表，所有查询参数都放在请求体中。"""
    users = [user.model_dump() for user in list_users(current_user, payload or UserListRequest())]
    return api_success(data=users, request_id=request.headers.get("x-request-id"))


@router.post("")
def post_user_create_alias(
    payload: UserCreateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """兼容早期 POST /users 调用，实际行为与 POST /users/create 一致。"""
    return post_user_create(payload, request, current_user)


@router.post("/create")
def post_user_create(
    payload: UserCreateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """创建用户账号，导师和管理员权限边界由服务层校验。"""
    user = create_user(current_user, payload)
    return api_success(data=user.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/update")
def post_user_update(
    payload: UserUpdateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """更新平台用户资料、统一识别码以外的账号字段、角色或状态。"""
    user = update_user(current_user, payload)
    return api_success(data=user.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/password/reset")
def post_user_password_reset(
    payload: UserPasswordResetRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """由管理端重置指定用户密码。"""
    user = reset_user_password(current_user, payload)
    return api_success(data=user.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/delete")
def post_user_delete(
    payload: UserDeleteRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """删除平台用户，并由服务层同步处理 Linux 子账户和最后管理员保护。"""
    user = delete_user(current_user, payload.user_id)
    return api_success(data=user.model_dump(), request_id=request.headers.get("x-request-id"))


@router.delete("/{user_id}")
def delete_user_by_path(
    user_id: int,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """兼容早期 DELETE /users/{user_id} 调用。"""
    user = delete_user(current_user, user_id)
    return api_success(data=user.model_dump(), request_id=request.headers.get("x-request-id"))
