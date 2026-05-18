from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.files import FileOperationRequest
from app.services.auth_service import UserRecord
from app.services.file_service import acknowledge_write_operation, list_files, preview_file

router = APIRouter()


@router.get("/list")
def get_file_list(
    request: Request,
    path: str = "/workspace",
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
    """预览文本文件内容，二进制预览后续按类型扩展。"""
    data = preview_file(current_user, path)
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/upload")
def post_file_upload(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """校验上传目标路径并返回占位确认，真实 multipart 上传后续接入。"""
    data = acknowledge_write_operation(current_user, "upload", payload.path, payload.target_path)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/mkdir")
def post_file_mkdir(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """校验创建目录请求并记录审计，真实落盘后续实现。"""
    data = acknowledge_write_operation(current_user, "mkdir", payload.path, payload.target_path)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/archive")
def post_file_archive(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """校验打包请求并记录审计，真实压缩任务后续实现。"""
    data = acknowledge_write_operation(current_user, "archive", payload.path, payload.target_path)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/extract")
def post_file_extract(
    payload: FileOperationRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """校验解压请求并记录审计，真实安全解压后续实现。"""
    data = acknowledge_write_operation(current_user, "extract", payload.path, payload.target_path)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))

