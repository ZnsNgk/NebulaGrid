from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.responses import api_success
from app.schemas.common import HealthData, RuntimeConfigData
from app.services.audit_service import external_nfs_configuration
from app.services.auth_service import UserRecord

router = APIRouter()


@router.get("/health")
def health_check(request: Request):
    """返回服务存活状态，用于 Nginx、systemd 和部署脚本做探活。"""
    settings = get_settings()
    data = HealthData(
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        status="ok",
    )
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/runtime-config")
def runtime_config(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """返回前端需要展示的运行时配置；登录校验防止未授权访客枚举服务器路径。"""
    settings = get_settings()
    external_nfs_enabled, external_nfs_path = external_nfs_configuration()
    data = RuntimeConfigData(
        shared_folder_root=settings.shared_folder_root,
        # 普通用户只需要知道入口是否可用，真实挂载路径绝不能进入该响应。
        external_nfs_enabled=external_nfs_enabled and bool(external_nfs_path),
    )
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))

