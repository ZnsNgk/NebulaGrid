from itertools import count
from pathlib import PurePosixPath

from app.core.config import get_settings
from app.core.errors import forbidden, not_found, validation_error
from app.core.rbac import require_permission
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
    """返回当前用户可使用的全部环境，并标记哪些环境允许当前用户修改。"""
    require_permission(user.role, "envs:read")
    return [mark_env_permissions(user, env) for env in _ENVS]


def upload_env_pack(user: UserRecord, payload: EnvUploadRequest) -> EnvInfo:
    """登记用户环境，并把真实路径固定到 miniconda 的 envs 目录一级子目录。"""
    require_permission(user.role, "envs:write")
    env_path = build_conda_env_path(payload.name, payload.path)
    if any(item.path == env_path or item.name == payload.name for item in _ENVS):
        raise validation_error("environment name already exists")
    env = EnvInfo(
        id=next(_ENV_ID),
        owner_user_id=user.id,
        name=payload.name,
        path=env_path,
        can_modify=True,
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
    """测试任意可用环境，环境使用权限与修改权限分离。"""
    env = get_env_for_user(user, env_id)
    return {"ok": True, "env_id": str(env.id), "state": env.state}


def upload_package(
    user: UserRecord,
    env_id: int,
    payload: EnvPackageUploadRequest,
) -> EnvPackageInfo:
    """登记环境包元数据；包安装属于修改环境，必须限制为环境所有者。"""
    env = get_env_for_user(user, env_id)
    require_env_owner(user, env)
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
    """创建环境包安装作业；只有环境所有者能修改目标环境。"""
    env = get_env_for_user(user, env_id)
    require_env_owner(user, env)
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
    """返回环境安装作业详情，并复用环境使用权限判断。"""
    job = require_job(job_id)
    get_env_for_user(user, job.env_id)
    return job


def get_install_job_log(user: UserRecord, job_id: int) -> str:
    """返回环境安装作业日志占位内容，真实版本会读取日志文件。"""
    job = get_install_job(user, job_id)
    return f"[env-install-job-{job.id}] status={job.status}\nlog storage is not connected yet\n"


def cancel_install_job(user: UserRecord, job_id: int) -> EnvInstallJobInfo:
    """取消自己创建的环境安装作业，并记录审计信息。"""
    job = get_install_job(user, job_id)
    if job.created_by != user.id:
        raise forbidden("job creator required")
    if job.status in {"succeeded", "failed", "cancelled"}:
        raise validation_error("finished job cannot be cancelled")
    job.status = "cancelled"
    job.finished_at = utc_now()
    record_audit(user.id, "env.install.cancel", "env_install_job", str(job.id))
    return job


def delete_env(user: UserRecord, env_id: int) -> EnvInfo:
    """删除自己创建的环境元数据；真实目录删除后续由受控作业处理，避免误删共享环境。"""
    env = get_env_for_user(user, env_id)
    require_env_owner(user, env)
    stored_env = next(item for item in _ENVS if item.id == env_id)
    _ENVS.remove(stored_env)
    record_audit(user.id, "env.delete", "env", str(env.id))
    return env


def get_env_for_user(user: UserRecord, env_id: int) -> EnvInfo:
    """获取用户可使用的环境；所有具备 envs:read 的用户都可使用全部环境。"""
    require_permission(user.role, "envs:read")
    env = next((item for item in _ENVS if item.id == env_id), None)
    if env is None:
        raise not_found("env not found")
    return mark_env_permissions(user, env)


def require_env_owner(user: UserRecord, env: EnvInfo) -> None:
    """断言用户可以修改该环境；任何角色都只修改自己创建的环境。"""
    if env.owner_user_id != user.id:
        raise forbidden("env owner required")


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


def build_conda_env_path(name: str, requested_path: str | None = None) -> str:
    """把环境名解析为 conda 可识别的一级环境目录，拒绝任何嵌套目录或路径逃逸。"""
    cleaned = name.strip()
    if cleaned in {"", ".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise validation_error("environment name must be a single directory name")
    root = PurePosixPath(get_settings().conda_env_root)
    env_path = str(root / cleaned)
    if requested_path and str(PurePosixPath(requested_path)) != env_path:
        raise validation_error("environment path must be under conda env root without extra directories")
    return env_path


def mark_env_permissions(user: UserRecord, env: EnvInfo) -> EnvInfo:
    """返回带当前用户修改权限标记的副本，避免把权限状态写回全局环境元数据。"""
    data = env.model_dump()
    data["can_modify"] = env.owner_user_id == user.id
    return EnvInfo(**data)
