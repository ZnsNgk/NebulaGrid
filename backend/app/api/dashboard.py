from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.services.auth_service import UserRecord
from app.services.dashboard_service import build_dashboard_summary

router = APIRouter()


@router.get("/summary")
def dashboard_summary(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """返回首页统计数据，占位实现用于稳定前后端契约。"""
    summary = build_dashboard_summary(current_user)
    return api_success(data=summary.model_dump(), request_id=request.headers.get("x-request-id"))
