from fastapi import APIRouter, Request

from app.core.config import get_settings
from app.core.responses import api_success
from app.schemas.common import HealthData

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

