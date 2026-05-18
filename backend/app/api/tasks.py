from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.tasks import TaskCreateRequest, TaskUpdateRequest
from app.services.auth_service import UserRecord
from app.services.task_service import (
    cancel_task,
    create_task,
    get_task_for_user,
    get_task_guard,
    get_task_log,
    list_tasks,
    resubmit_task,
    update_task,
)

router = APIRouter()


@router.get("")
def get_tasks(
    request: Request,
    state: str | None = None,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    current_user: UserRecord = Depends(get_current_user),
):
    """返回任务分页列表，支持状态和关键词筛选。"""
    items, total = list_tasks(current_user, state, search, page, page_size)
    data = {"items": [item.model_dump() for item in items], "total": total, "page": page, "page_size": page_size}
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("")
def post_task(
    payload: TaskCreateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """提交训练或脚本任务，并返回新建任务记录。"""
    task = create_task(current_user, payload)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/{task_id}")
def get_task(task_id: str, request: Request, current_user: UserRecord = Depends(get_current_user)):
    """返回任务详情，服务层会处理任务可见性。"""
    task = get_task_for_user(current_user, task_id)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.patch("/{task_id}")
def patch_task(
    task_id: str,
    payload: TaskUpdateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """编辑等待或挂起任务的可变字段。"""
    task = update_task(current_user, task_id, payload)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/{task_id}/cancel")
def post_cancel_task(task_id: str, request: Request, current_user: UserRecord = Depends(get_current_user)):
    """取消任务，后续执行器接入后会同时终止远端进程。"""
    task = cancel_task(current_user, task_id)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/{task_id}/resubmit")
def post_resubmit_task(task_id: str, request: Request, current_user: UserRecord = Depends(get_current_user)):
    """基于已有任务重新提交一条新任务。"""
    task = resubmit_task(current_user, task_id)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/{task_id}/log")
def get_log(
    task_id: str,
    tail: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
):
    """返回任务日志尾部文本，当前为日志系统接入前的占位实现。"""
    return PlainTextResponse(get_task_log(current_user, task_id, tail))


@router.get("/{task_id}/log/download")
def download_log(task_id: str, current_user: UserRecord = Depends(get_current_user)):
    """返回完整任务日志文本，后续会替换为文件下载响应。"""
    return PlainTextResponse(get_task_log(current_user, task_id, tail=None))


@router.get("/{task_id}/guard")
def get_guard(task_id: str, request: Request, current_user: UserRecord = Depends(get_current_user)):
    """返回任务运行时守护检测摘要。"""
    guard = get_task_guard(current_user, task_id)
    return api_success(data=guard.model_dump(), request_id=request.headers.get("x-request-id"))

