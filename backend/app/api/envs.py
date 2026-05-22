from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.schemas.envs import EnvArchiveImportRequest, EnvCloneRequest, EnvInstalledPackageDeleteRequest, EnvLocalPackageInstallRequest, EnvPackageInstallRequest, EnvPackageUploadRequest, EnvUploadRequest
from app.services.auth_service import UserRecord
from app.services.env_service import (
    cancel_install_job,
    clone_env,
    delete_env,
    delete_installed_packages,
    delete_package,
    get_env_operation_log,
    list_env_compile_targets,
    get_install_job,
    get_install_job_log,
    import_env_archive,
    import_detected_envs,
    install_package,
    install_local_package,
    list_envs,
    preview_delete_installed_packages,
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


@router.post("/import-detected")
def post_import_detected_envs(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """扫描当前 conda 环境目录并把环境信息写入数据库。"""
    envs = import_detected_envs(current_user)
    return api_success(data=[env.model_dump() for env in envs], request_id=request.headers.get("x-request-id"))


@router.post("/import-archive")
def post_import_env_archive(
    payload: EnvArchiveImportRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """从用户文件根目录选择 zip 包并导入为可用 conda 环境。"""
    env = import_env_archive(current_user, payload)
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
    return api_success(data=data.model_dump(), request_id=request.headers.get("x-request-id"))


@router.get("/{env_id}/log")
def get_env_log(env_id: int, current_user: UserRecord = Depends(get_current_user)):
    """读取单个环境的落盘操作日志。"""
    return PlainTextResponse(get_env_operation_log(current_user, env_id))


@router.post("/{env_id}/clone")
def post_clone_env(
    env_id: int,
    payload: EnvCloneRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """基于已有环境创建当前用户自己的副本。"""
    env = clone_env(current_user, env_id, payload)
    return api_success(data=env.model_dump(), request_id=request.headers.get("x-request-id"))


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


@router.get("/compile-targets")
def get_compile_targets(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """每次打开编译安装弹窗时实时返回主节点和用户可见节点的编译器/GPU 信息。"""
    targets = [target.model_dump() for target in list_env_compile_targets(current_user)]
    return api_success(data=targets, request_id=request.headers.get("x-request-id"))


@router.post("/{env_id}/packages/install")
def post_local_package_install(
    env_id: int,
    payload: EnvLocalPackageInstallRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """创建本机离线安装作业，实际安装由 env_install_worker 后台执行。"""
    result = install_local_package(current_user, env_id, payload)
    return api_success(data=result.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/{env_id}/packages/delete-preview")
def post_installed_package_delete_preview(
    env_id: int,
    payload: EnvInstalledPackageDeleteRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """返回已安装包删除确认内容，前端需让用户确认后再执行删除。"""
    preview = preview_delete_installed_packages(current_user, env_id, payload)
    return api_success(data=preview.model_dump(), request_id=request.headers.get("x-request-id"))


@router.post("/{env_id}/packages/delete")
def post_installed_package_delete(
    env_id: int,
    payload: EnvInstalledPackageDeleteRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """删除目标环境内已安装的 conda/pip 包。"""
    result = delete_installed_packages(current_user, env_id, payload)
    return api_success(data=result.model_dump(), request_id=request.headers.get("x-request-id"))


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


@router.delete("/{env_id}/packages/{package_id}")
def delete_env_package(
    env_id: int,
    package_id: int,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
):
    """删除环境包登记并记录到该环境的操作日志。"""
    package = delete_package(current_user, env_id, package_id)
    return api_success(data=package.model_dump(), request_id=request.headers.get("x-request-id"))


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
