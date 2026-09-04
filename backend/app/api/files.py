from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.files import FileContentRequest, FileOperationRequest
from app.services.auth_service import UserRecord, get_user_by_token
from app.services.file_service import (
    build_download_path,
    build_preview_stream_path,
    copy_path,
    create_directory,
    create_text_file,
    delete_path,
    get_latest_file_job,
    grant_execute_permission,
    guess_content_type,
    list_files,
    move_path,
    preview_file,
    save_upload_file,
    save_text_file,
    start_archive_job,
    start_extract_job,
)
from app.services.file_executor import run_file_operation

router = APIRouter()
MEDIA_STREAM_CHUNK_SIZE = 1024 * 1024


@router.get("/list")
async def get_file_list(
    request: Request,
    path: str = "/",
    scope: str = "",
    current_user: UserRecord = Depends(get_current_user),
):
    """列出虚拟路径下的文件和目录。"""
    data = await run_file_operation(list_files, current_user, path, scope)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/preview")
async def get_file_preview(
    request: Request,
    path: str,
    scope: str = "",
    full: bool = False,
    current_user: UserRecord = Depends(get_current_user),
):
    """预览文件；超限文本只有在 full=true 时才主动读取完整内容。"""
    data = await run_file_operation(preview_file, current_user, path, scope, full)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/download")
async def get_file_download(
    path: str,
    scope: str = "",
    current_user: UserRecord = Depends(get_current_user),
):
    """下载单个文件；目录需要先通过 archive 接口打包，避免隐式遍历。"""
    real_path = await run_file_operation(build_download_path, current_user, path, scope)
    return FileResponse(real_path, filename=real_path.name)


@router.get("/media")
async def get_file_media(
    request: Request,
    path: str,
    token: str = Query(min_length=1),
    scope: str = "",
):
    """按 HTTP Range 返回图片、视频、PDF 或 Office 转换结果；查询令牌用于嵌入元素。"""
    current_user = get_user_by_token(token)
    real_path = await run_file_operation(build_preview_stream_path, current_user, path, scope)
    return build_media_stream_response(real_path, request.headers.get("range"))


@router.get("/jobs/latest")
async def get_latest_job(
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """返回当前用户最近一次打包或解压任务，供刷新页面后恢复进度条。"""
    data = await run_file_operation(get_latest_file_job, current_user)
    return api_success(data=data.model_dump() if data else None, request_id=request.headers.get("x-request-id"))


@router.post("/upload")
async def post_file_upload(
    request: Request,
    path: str = Form("/"),
    file: UploadFile = File(...),
    current_user: UserRecord = Depends(get_current_user),
):
    """把 multipart 文件上传到当前目录；同名文件默认拒绝覆盖。"""
    data = await run_file_operation(save_upload_file, current_user, path, file.filename or "", file.file)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/mkdir")
async def post_file_mkdir(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """创建目录，path 表示要创建的完整虚拟目录路径。"""
    data = await run_file_operation(create_directory, current_user, payload.path)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/create")
async def post_file_create(
    payload: FileContentRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """创建文本文件，已存在时拒绝覆盖。"""
    data = await run_file_operation(create_text_file, current_user, payload.path, payload.content)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/save")
async def post_file_save(
    payload: FileContentRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """保存文本文件内容，供前端编辑器使用。"""
    data = await run_file_operation(save_text_file, current_user, payload.path, payload.content)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/permissions/execute")
async def post_file_execute_permission(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """为普通文件授予执行权限，供用户上传脚本后提交任务前处理。"""
    data = await run_file_operation(grant_execute_permission, current_user, payload.path)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/copy")
async def post_file_copy(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """复制文件或目录，target_path 表示完整目标路径。"""
    data = await run_file_operation(
        copy_path,
        current_user,
        payload.path,
        require_target_path(payload),
        payload.scope,
        payload.target_scope,
    )
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/move")
async def post_file_move(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """移动文件或目录，target_path 表示完整目标路径。"""
    data = await run_file_operation(move_path, current_user, payload.path, require_target_path(payload))
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/rename")
async def post_file_rename(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """重命名本质是同目录移动，仍走统一的移动和审计逻辑。"""
    data = await run_file_operation(move_path, current_user, payload.path, require_target_path(payload))
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/archive")
async def post_file_archive(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """启动文件夹 zip 打包任务；同一用户同时只能有一个重 IO 文件任务。"""
    data = await run_file_operation(start_archive_job, current_user, payload.path, payload.target_path)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/extract")
async def post_file_extract(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """启动 zip/tar 系列压缩包解压任务；目标目录必须由服务端路径解析确认在用户边界内。"""
    data = await run_file_operation(start_extract_job, current_user, payload.path, payload.target_path)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.delete("")
async def delete_file(
    request: Request,
    path: str,
    current_user: UserRecord = Depends(get_current_user),
):
    """删除文件或目录；服务层会保护工作区根和可见根。"""
    data = await run_file_operation(delete_path, current_user, path)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


def require_target_path(payload: FileOperationRequest) -> str:
    """集中校验 target_path，保持 API 层错误语义一致。"""
    if not payload.target_path:
        from app.core.errors import validation_error

        raise validation_error("target_path is required")
    return payload.target_path


def build_media_stream_response(path: Path, range_header: str | None) -> Response:
    """生成支持单段字节范围的响应，让浏览器读取图片、视频或文档展示所需的片段。"""
    size = path.stat().st_size
    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=300",
        "Referrer-Policy": "no-referrer",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
    }
    if not range_header:
        common_headers["Content-Length"] = str(size)
        return StreamingResponse(
            iterate_file_range(path, 0, max(0, size - 1)),
            media_type=guess_content_type(path),
            headers=common_headers,
        )

    requested_range = parse_byte_range(range_header, size)
    if requested_range is None:
        return Response(
            status_code=416,
            headers={**common_headers, "Content-Range": f"bytes */{size}"},
        )

    start, end = requested_range
    headers = {
        **common_headers,
        "Content-Length": str(end - start + 1),
        "Content-Range": f"bytes {start}-{end}/{size}",
    }
    return StreamingResponse(
        iterate_file_range(path, start, end),
        status_code=206,
        media_type=guess_content_type(path),
        headers=headers,
    )


def parse_byte_range(value: str, size: int) -> tuple[int, int] | None:
    """解析浏览器常用的单段 bytes Range；多段或越界请求返回不可满足。"""
    unit, separator, raw_range = value.strip().partition("=")
    if unit.lower() != "bytes" or not separator or "," in raw_range or "-" not in raw_range or size <= 0:
        return None
    start_text, end_text = (part.strip() for part in raw_range.split("-", 1))
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            return max(0, size - suffix_length), size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def iterate_file_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    """分块读取指定闭区间，客户端断开时生成器关闭文件，不把完整媒体文件放进内存。"""
    remaining = max(0, end - start + 1)
    with path.open("rb") as file:
        file.seek(start)
        while remaining:
            chunk = file.read(min(MEDIA_STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
