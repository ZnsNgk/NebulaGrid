import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.db.session import SessionLocal, get_db
from app.schemas.tasks import TaskBatchCreateRequest, TaskCreateRequest, TaskUpdateRequest
from app.services.auth_service import UserRecord
from app.services.task_service import (
    cancel_task,
    create_task,
    create_tasks_batch,
    delete_task,
    get_task_for_user,
    get_task_guard,
    get_task_log,
    hold_task,
    list_tasks,
    preview_task_delete,
    resubmit_task,
    task_change_cursor,
    update_task,
)
from app.services.auth_service import get_user_by_token

router = APIRouter()


@router.get("")
def get_tasks(
    request: Request,
    state: str | None = None,
    search: str | None = None,
    all_history: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回任务分页列表，服务层会按角色过滤可见范围。"""
    items, total = list_tasks(current_user, db, state, search, page, page_size, all_history=all_history)
    data = {"items": [item.model_dump() for item in items], "total": total, "page": page, "page_size": page_size}
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("")
def post_task(
    payload: TaskCreateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """提交单个训练或脚本任务，初始进入等待区或挂起区。"""
    task = create_task(current_user, payload, db)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/batch")
def post_tasks_batch(
    payload: TaskBatchCreateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """批量提交任务：每个有效命令行生成一条任务。"""
    result = create_tasks_batch(current_user, payload, db)
    return api_success(data=result.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/events")
async def stream_task_events(
    request: Request,
    token: str = Query(min_length=1),
):
    """推送当前用户可见任务的轻量变化事件，前端据此刷新当前任务区。"""
    current_user = get_user_by_token(token)

    async def event_generator():
        previous: dict[str, int] | None = None
        while not await request.is_disconnected():
            with SessionLocal() as db:
                cursor = task_change_cursor(current_user, db)
            if cursor != previous:
                previous = cursor
                yield f"event: tasks\ndata: {json.dumps(cursor, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)


@router.get("/{task_id}")
def get_task(
    task_id: str,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回任务详情；不可见任务按不存在处理。"""
    task = get_task_for_user(current_user, task_id, db)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.patch("/{task_id}")
def patch_task(
    task_id: str,
    payload: TaskUpdateRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """编辑等待、挂起或历史任务，运行中任务不可编辑。"""
    task = update_task(current_user, task_id, payload, db)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/{task_id}/hold")
def post_hold_task(
    task_id: str,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """切换等待区任务的挂起状态，挂起任务不会被调度器分配资源。"""
    task = hold_task(current_user, task_id, db)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/{task_id}/cancel")
def post_cancel_task(
    task_id: str,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """请求停止任务，并写入任务事件和审计日志；远端确认后才进入终态。"""
    task = cancel_task(current_user, task_id, db)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/{task_id}/resubmit")
def post_resubmit_task(
    task_id: str,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """基于已有任务重新提交一条新任务，使用新的任务 ID。"""
    task = resubmit_task(current_user, task_id, db)
    return api_success(data=task.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/{task_id}/delete-preview")
def get_delete_preview(
    task_id: str,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除前返回所有后继任务 ID，供前端二次确认是否一并删除。"""
    preview = preview_task_delete(current_user, task_id, db)
    return api_success(data=preview.model_dump(), request_id=request.headers.get("x-request-id"))


@router.delete("/{task_id}")
def delete_task_record(
    task_id: str,
    request: Request,
    delete_successors: bool = False,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除等待区或历史区任务，可按用户确认递归删除后继任务。"""
    result = delete_task(current_user, task_id, delete_successors, db)
    return api_success(data=result.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/{task_id}/log")
def get_log(
    task_id: str,
    tail: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回任务日志尾部文本。"""
    return PlainTextResponse(get_task_log(current_user, task_id, tail, db))


@router.get("/{task_id}/log/events")
async def stream_task_log_events(
    task_id: str,
    request: Request,
    token: str = Query(min_length=1),
    tail_bytes: int = Query(default=8192, ge=0, le=1024 * 1024),
):
    """按 SSE 推送任务日志增量，供前端实现实时 tail。"""
    current_user = get_user_by_token(token)
    with SessionLocal() as db:
        task = get_task_for_user(current_user, task_id, db)
    log_path = Path(task.log_path)

    async def event_generator():
        offset = initial_log_offset(log_path, tail_bytes)
        while not await request.is_disconnected():
            if log_path.is_file():
                size = log_path.stat().st_size
                if size < offset:
                    offset = 0
                if size > offset:
                    with log_path.open("r", encoding="utf-8", errors="replace") as file:
                        file.seek(offset)
                        chunk = file.read()
                        offset = file.tell()
                    if chunk:
                        yield f"event: log\ndata: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def initial_log_offset(log_path: Path, tail_bytes: int) -> int:
    """新订阅默认从尾部一小段开始，避免大日志首次连接阻塞浏览器。"""
    if not log_path.is_file():
        return 0
    return max(0, log_path.stat().st_size - tail_bytes)


@router.get("/{task_id}/log/download")
def download_log(
    task_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回完整任务日志文本；生产下载可后续替换为文件响应。"""
    return PlainTextResponse(get_task_log(current_user, task_id, tail=None, db=db))


@router.get("/{task_id}/guard")
def get_guard(
    task_id: str,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回任务运行时守护检测摘要。"""
    guard = get_task_guard(current_user, task_id, db)
    return api_success(data=guard.model_dump(), request_id=request.headers.get("x-request-id"))
