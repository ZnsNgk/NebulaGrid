from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.users import UserCreateRequest
from app.services.auth_service import UserRecord
from app.services.user_service import create_user, delete_user, list_users

router = APIRouter()


@router.get("")
def get_users(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """返回用户管理列表，服务层负责角色范围控制。"""
    users = [user.model_dump() for user in list_users(current_user)]
    return api_success(data=users, request_id=request.headers.get("x-request-id"))


@router.post("")
def post_user(
    payload: UserCreateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """创建用户账号，导师和管理员权限边界由服务层校验。"""
    user = create_user(current_user, payload)
    return api_success(data=user.model_dump(), request_id=request.headers.get("x-request-id"))


@router.delete("/{user_id}")
def delete_user_account(
    user_id: int,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """删除平台用户，并由服务层同步处理 Linux 子账户和最后管理员保护。"""
    user = delete_user(current_user, user_id)
    return api_success(data=user.model_dump(), request_id=request.headers.get("x-request-id"))
