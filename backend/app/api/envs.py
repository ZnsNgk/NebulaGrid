from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.envs import EnvPackageInstallRequest, EnvPackageUploadRequest, EnvUploadRequest
from app.services.auth_service import UserRecord
from app.services.env_service import (
    cancel_install_job,
    delete_env,
    get_install_job,
    get_install_job_log,
    install_package,
    list_envs,
    test_env,
    upload_env_pack,
    upload_package,
)

router = APIRouter()
jobs_router = APIRouter()


@router.get("")
def get_envs(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """返回当前用户可见的环境列表。"""
    envs = [env.model_dump() for env in list_envs(current_user)]
    return api_success(data=envs, request_id=request.headers.get("x-request-id"))


@router.post("/upload-pack")
@router.post("/register")
@router.post("/import-conda-pack")
def post_upload_pack(
    payload: EnvUploadRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """登记 conda-pack 环境导入请求。"""
    env = upload_env_pack(current_user, payload)
    return api_success(data=env.model_dump(), request_id=request.headers.get("x-request-id"))


@router.delete("/{env_id}")
def delete_registered_env(env_id: int, request: Request, current_user: UserRecord = Depends(get_current_user)):
    """删除当前用户自己创建的环境登记信息。"""
    env = delete_env(current_user, env_id)
    return api_success(data=env.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/{env_id}/test")
def post_test_env(env_id: int, request: Request, current_user: UserRecord = Depends(get_current_user)):
    """触发环境测试占位动作。"""
    data = test_env(current_user, env_id)
    return api_success(data=data, request_id=request.headers.get("x-request-id"))


@router.post("/{env_id}/packages/upload")
def post_package_upload(
    env_id: int,
    payload: EnvPackageUploadRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """登记 whl 或源码包上传元数据。"""
    package = upload_package(current_user, env_id, payload)
    return api_success(data=package.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/{env_id}/packages/{package_id}/install")
def post_package_install(
    env_id: int,
    package_id: int,
    payload: EnvPackageInstallRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """创建环境包安装或编译安装作业。"""
    job = install_package(current_user, env_id, package_id, payload)
    return api_success(data=job.model_dump(), request_id=request.headers.get("x-request-id"))


@jobs_router.get("/{job_id}")
def get_env_install_job(job_id: int, request: Request, current_user: UserRecord = Depends(get_current_user)):
    """查询环境安装作业详情。"""
    job = get_install_job(current_user, job_id)
    return api_success(data=job.model_dump(), request_id=request.headers.get("x-request-id"))


@jobs_router.get("/{job_id}/log")
def get_env_install_log(job_id: int, current_user: UserRecord = Depends(get_current_user)):
    """返回环境安装作业日志文本。"""
    return PlainTextResponse(get_install_job_log(current_user, job_id))


@jobs_router.post("/{job_id}/cancel")
def post_cancel_env_install_job(
    job_id: int,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """取消环境安装作业。"""
    job = cancel_install_job(current_user, job_id)
    return api_success(data=job.model_dump(), request_id=request.headers.get("x-request-id"))
