from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.auth import LoginRequest
from app.services.auth_service import (
    authenticate_user,
    build_public_user,
    UserRecord,
)

router = APIRouter()


@router.post("/login")
def login(payload: LoginRequest, request: Request):
    """校验账号密码并签发临时令牌，MVP 阶段先使用内存用户便于前端联调。"""
    login_result = authenticate_user(payload.identity, payload.password)
    return api_success(data=login_result.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/logout")
def logout(request: Request):
    """提供退出登录占位接口，后续接入服务端会话或令牌吊销表。"""
    return api_success(data={"logged_out": True}, request_id=request.headers.get("x-request-id"))


@router.get("/me")
def me(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """根据 Authorization 令牌返回当前用户资料和权限列表。"""
    return api_success(data=build_public_user(current_user).model_dump(), request_id=request.headers.get("x-request-id"))
