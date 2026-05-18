from itertools import count

from app.core.config import get_settings
from app.core.errors import forbidden, not_found, validation_error
from app.core.path_resolver import resolve_user_visible_path
from app.core.rbac import Role, require_permission
from app.schemas.envs import (
    EnvInfo,
    EnvInstallJobInfo,
    EnvPackageInfo,
    EnvPackageInstallRequest,
    EnvPackageUploadRequest,
    EnvUploadRequest,
)
from app.services.audit_service import record_audit, utc_now
from app.services.auth_service import UserRecord

_ENV_ID = count(1)
_PACKAGE_ID = count(1)
_JOB_ID = count(1)
_ENVS: list[EnvInfo] = []
_PACKAGES: list[EnvPackageInfo] = []
_JOBS: list[EnvInstallJobInfo] = []


def list_envs(user: UserRecord) -> list[EnvInfo]:
    """返回用户可见环境列表，管理员和导师可先查看全部占位数据。"""
    require_permission(user.role, "envs:read")
    if user.role in {Role.ADMIN, Role.MENTOR, Role.VIEWER}:
        return _ENVS
    return [env for env in _ENVS if env.owner_user_id == user.id]


def upload_env_pack(user: UserRecord, payload: EnvUploadRequest) -> EnvInfo:
    """登记 conda-pack 环境元数据，真实上传和解压导入后续接入。"""
    require_permission(user.role, "envs:write")
    resolve_user_visible_path(payload.path, user.id)
    env = EnvInfo(
        id=next(_ENV_ID),
        owner_user_id=user.id,
        name=payload.name,
        path=payload.path,
        description=payload.description,
        source_type="conda_pack",
        state="registered",
        python_version=payload.python_version,
        created_at=utc_now(),
    )
    _ENVS.append(env)
    record_audit(user.id, "env.upload_pack", "env", str(env.id))
    return env


def test_env(user: UserRecord, env_id: int) -> dict[str, str | bool]:
    """触发环境测试占位动作，真实版本会调用远端 Python 版本探测。"""
    env = get_env_for_user(user, env_id)
    return {"ok": True, "env_id": str(env.id), "state": env.state}


def upload_package(
    user: UserRecord,
    env_id: int,
    payload: EnvPackageUploadRequest,
) -> EnvPackageInfo:
    """登记环境包元数据，后续替换为真实文件上传、校验和落盘。"""
    env = get_env_for_user(user, env_id)
    require_env_owner_or_admin(user, env)
    settings = get_settings()
    package = EnvPackageInfo(
        id=next(_PACKAGE_ID),
        env_id=env.id,
        owner_user_id=user.id,
        filename=payload.filename,
        package_type=payload.package_type,
        file_path=f"{settings.env_package_root}/{user.username}/{payload.filename}",
        size_bytes=payload.size_bytes,
        sha256=payload.sha256,
        status="uploaded",
        created_at=utc_now(),
    )
    _PACKAGES.append(package)
    record_audit(user.id, "env.package.upload", "env_package", str(package.id))
    return package


def install_package(
    user: UserRecord,
    env_id: int,
    package_id: int,
    payload: EnvPackageInstallRequest,
) -> EnvInstallJobInfo:
    """创建环境包安装作业，MVP 阶段只入队不执行远端命令。"""
    env = get_env_for_user(user, env_id)
    require_env_owner_or_admin(user, env)
    package = require_package(package_id)
    if package.env_id != env.id:
        raise validation_error("package does not belong to env")
    job = EnvInstallJobInfo(
        id=next(_JOB_ID),
        package_id=package.id,
        env_id=env.id,
        mode=payload.mode,
        target_node_id=payload.target_node_id,
        visible_gpu_indices=payload.visible_gpu_indices,
        status="queued",
        log_path=f"{get_settings().env_install_log_root}/job-{package.id}.log",
        created_by=user.id,
    )
    _JOBS.append(job)
    record_audit(user.id, "env.package.install", "env_install_job", str(job.id))
    return job


def get_install_job(user: UserRecord, job_id: int) -> EnvInstallJobInfo:
    """返回环境安装作业详情，并复用环境可见性判断。"""
    job = require_job(job_id)
    get_env_for_user(user, job.env_id)
    return job


def get_install_job_log(user: UserRecord, job_id: int) -> str:
    """返回环境安装作业日志占位内容，真实版本会读取日志文件。"""
    job = get_install_job(user, job_id)
    return f"[env-install-job-{job.id}] status={job.status}\nlog storage is not connected yet\n"


def cancel_install_job(user: UserRecord, job_id: int) -> EnvInstallJobInfo:
    """取消等待或运行中的环境安装作业，并记录审计信息。"""
    job = get_install_job(user, job_id)
    if job.created_by != user.id and user.role != Role.ADMIN:
        raise forbidden("job creator or admin required")
    if job.status in {"succeeded", "failed", "cancelled"}:
        raise validation_error("finished job cannot be cancelled")
    job.status = "cancelled"
    job.finished_at = utc_now()
    record_audit(user.id, "env.install.cancel", "env_install_job", str(job.id))
    return job


def get_env_for_user(user: UserRecord, env_id: int) -> EnvInfo:
    """获取用户可见环境，不可见时返回 NOT_FOUND 以减少越权探测。"""
    env = next((item for item in _ENVS if item.id == env_id), None)
    if env is None or (user.role not in {Role.ADMIN, Role.MENTOR, Role.VIEWER} and env.owner_user_id != user.id):
        raise not_found("env not found")
    return env


def require_env_owner_or_admin(user: UserRecord, env: EnvInfo) -> None:
    """断言用户是环境所有者或管理员，失败时返回 403。"""
    if user.role != Role.ADMIN and env.owner_user_id != user.id:
        raise forbidden("env owner or admin required")


def require_package(package_id: int) -> EnvPackageInfo:
    """按 ID 获取环境包元数据，找不到时抛出 NOT_FOUND。"""
    package = next((item for item in _PACKAGES if item.id == package_id), None)
    if package is None:
        raise not_found("package not found")
    return package


def require_job(job_id: int) -> EnvInstallJobInfo:
    """按 ID 获取环境安装作业，找不到时抛出 NOT_FOUND。"""
    job = next((item for item in _JOBS if item.id == job_id), None)
    if job is None:
        raise not_found("install job not found")
    return job
