import shutil

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.files import FileContentRequest, FileOperationRequest
from app.services.auth_service import UserRecord
from app.services.file_service import (
    build_download_path,
    complete_upload,
    copy_path,
    create_directory,
    create_text_file,
    delete_path,
    get_latest_file_job,
    list_files,
    move_path,
    preview_file,
    resolve_upload_target,
    save_text_file,
    start_archive_job,
    start_extract_job,
)

router = APIRouter()


@router.get("/list")
def get_file_list(
    request: Request,
    path: str = "/",
    current_user: UserRecord = Depends(get_current_user),
):
    """列出虚拟路径下的文件和目录。"""
    data = list_files(current_user, path)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/preview")
def get_file_preview(
    request: Request,
    path: str,
    current_user: UserRecord = Depends(get_current_user),
):
    """预览文本、图片、音视频等文件；文本内容可被前端编辑保存。"""
    data = preview_file(current_user, path)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/download")
def get_file_download(
    path: str,
    current_user: UserRecord = Depends(get_current_user),
):
    """下载单个文件；目录需要先通过 archive 接口打包，避免隐式遍历。"""
    real_path = build_download_path(current_user, path)
    return FileResponse(real_path, filename=real_path.name)


@router.get("/jobs/latest")
def get_latest_job(
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """返回当前用户最近一次打包或解压任务，供刷新页面后恢复进度条。"""
    data = get_latest_file_job(current_user)
    return api_success(data=data.model_dump() if data else None, request_id=request.headers.get("x-request-id"))


@router.post("/upload")
def post_file_upload(
    request: Request,
    path: str = Form("/"),
    file: UploadFile = File(...),
    current_user: UserRecord = Depends(get_current_user),
):
    """把 multipart 文件上传到当前目录；同名文件默认拒绝覆盖。"""
    target = resolve_upload_target(current_user, path, file.filename or "")
    with target.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    data = complete_upload(current_user, path, target.name)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/mkdir")
def post_file_mkdir(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """创建目录，path 表示要创建的完整虚拟目录路径。"""
    data = create_directory(current_user, payload.path)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/create")
def post_file_create(
    payload: FileContentRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """创建文本文件，已存在时拒绝覆盖。"""
    data = create_text_file(current_user, payload.path, payload.content)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/save")
def post_file_save(
    payload: FileContentRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """保存文本文件内容，供前端编辑器使用。"""
    data = save_text_file(current_user, payload.path, payload.content)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/copy")
def post_file_copy(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """复制文件或目录，target_path 表示完整目标路径。"""
    data = copy_path(current_user, payload.path, require_target_path(payload))
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/move")
def post_file_move(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """移动文件或目录，target_path 表示完整目标路径。"""
    data = move_path(current_user, payload.path, require_target_path(payload))
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/rename")
def post_file_rename(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """重命名本质是同目录移动，仍走统一的移动和审计逻辑。"""
    data = move_path(current_user, payload.path, require_target_path(payload))
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/archive")
def post_file_archive(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """启动文件夹 zip 打包任务；同一用户同时只能有一个重 IO 文件任务。"""
    data = start_archive_job(current_user, payload.path, payload.target_path)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/extract")
def post_file_extract(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """启动 zip/tar 系列压缩包解压任务；目标目录必须由服务端路径解析确认在用户边界内。"""
    data = start_extract_job(current_user, payload.path, payload.target_path)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.delete("")
def delete_file(
    request: Request,
    path: str,
    current_user: UserRecord = Depends(get_current_user),
):
    """删除文件或目录；服务层会保护工作区根和可见根。"""
    data = delete_path(current_user, path)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


def require_target_path(payload: FileOperationRequest) -> str:
    """集中校验 target_path，保持 API 层错误语义一致。"""
    if not payload.target_path:
        from app.core.errors import validation_error

        raise validation_error("target_path is required")
    return payload.target_path
