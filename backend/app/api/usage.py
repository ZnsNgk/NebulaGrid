"""用量统计只读入口；范围在服务端按当前账号重新计算。"""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.db.session import get_db
from app.services.auth_service import UserRecord
from app.services.usage_service import build_task_usage, build_node_usage

router = APIRouter()


@router.get("/tasks")
def task_usage(request: Request, current_user: UserRecord = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """全量汇总可见任务，不受任务列表分页数量限制。"""
    return api_success(data=build_task_usage(current_user, db),
                       request_id=request.headers.get("x-request-id"))


@router.get("/nodes")
def node_usage(request: Request, days: Literal["7", "30", "90"] = Query("7"),
               current_user: UserRecord = Depends(get_current_user), db: Session = Depends(get_db)):
    """限制日期跨度，防止任意长周期扫描拖慢监控服务。"""
    return api_success(data=build_node_usage(current_user, db, int(days)),
                       request_id=request.headers.get("x-request-id"))
