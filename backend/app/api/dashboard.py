from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.db.session import get_db
from app.services.auth_service import UserRecord
from app.services.dashboard_service import build_dashboard_summary, build_presenter_dashboard

router = APIRouter()


@router.get("/summary")
def dashboard_summary(
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回首页统计数据，占位实现用于稳定前后端契约。"""
    summary = build_dashboard_summary(current_user, db)
    return api_success(data=summary.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/presenter")
def presenter_dashboard(
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回展示者大屏聚合数据：全局统计、所有节点状态和 InfluxDB 历史曲线。"""
    dashboard = build_presenter_dashboard(current_user, db)
    return api_success(data=dashboard.model_dump(), request_id=request.headers.get("x-request-id"))
