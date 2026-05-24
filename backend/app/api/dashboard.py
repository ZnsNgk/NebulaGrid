from fastapi import APIRouter, Depends, Query, Request
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
    hours: str | None = Query(None, description="展示者大屏历史监控小时数，支持 1/3/6/12/24 或 1h/3h/6h/12h/24h"),
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回展示者大屏聚合数据：全局统计、所有节点状态和 InfluxDB 历史曲线。"""
    dashboard = build_presenter_dashboard(current_user, db, history_hours=parse_presenter_hours(hours))
    return api_success(data=dashboard.model_dump(), request_id=request.headers.get("x-request-id"))


def parse_presenter_hours(value: str | None) -> int:
    """解析展示者历史范围；缺省或异常值回退到 1 小时，避免参数错误阻断大屏刷新。"""
    if value is None:
        return 1
    normalized = value.strip().lower().removesuffix("h")
    if not normalized:
        return 1
    try:
        hours = int(normalized)
    except ValueError:
        return 1
    return hours if hours in {1, 3, 6, 12, 24} else 1
