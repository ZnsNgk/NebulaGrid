from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.files import FileContentRequest, FileOperationRequest
from app.services.auth_service import UserRecord
from app.services.file_service import (
    build_download_path,
    copy_path,
    create_directory,
    create_text_file,
    delete_path,
    get_latest_file_job,
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
    current_user: UserRecord = Depends(get_current_user),
):
    """预览文本、图片、音视频等文件；文本内容可被前端编辑保存。"""
    data = await run_file_operation(preview_file, current_user, path, scope)
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
