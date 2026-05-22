import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import zipfile
from itertools import count
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.errors import forbidden, not_found, validation_error
from app.core.path_resolver import normalize_virtual_path, resolve_user_visible_path
from app.core.rbac import Role, require_permission
from app.core.time_utils import ensure_local_datetime, local_datetime
from app.db.models import Env, EnvInstallJob, EnvOperationLog, EnvPackage, EnvPackageManifest, Task, User
from app.db.session import SessionLocal
from app.schemas.envs import (
    EnvArchiveImportRequest,
    EnvCloneRequest,
    EnvFrameworkInfo,
    EnvInfo,
    EnvInstallJobInfo,
    EnvInstalledPackageDeletePreview,
    EnvInstalledPackageDeleteRequest,
    EnvInstalledPackageDeleteResult,
    EnvLocalPackageInstallRequest,
    EnvLocalPackageInstallResult,
    EnvPackageInfo,
    EnvPackageInstallRequest,
    EnvPackageVersion,
    EnvTestResult,
    EnvPackageUploadRequest,
    EnvUploadRequest,
)
from app.services.audit_service import record_audit, utc_now
from app.services.auth_service import UserRecord

_PACKAGE_ID = count(1)
_JOB_ID = count(1)
_PACKAGES: list[EnvPackageInfo] = []
_JOBS: list[EnvInstallJobInfo] = []
ENV_IMPORT_ACTIVE_STATES = {"copying", "importing", "fixing", "testing"}
PROTECTED_ENV_PACKAGE_NAMES = {
    "python",
    "pip",
    "setuptools",
    "wheel",
    "conda",
    "conda-package-handling",
    "conda-package-streaming",
    "openssl",
    "sqlite",
    "tk",
    "xz",
    "zlib",
    "libffi",
    "ncurses",
    "readline",
    "ca-certificates",
    "certifi",
}


def list_envs(user: UserRecord) -> list[EnvInfo]:
    """执行 conda 环境列表扫描并返回当前可用环境，保证进入页面和刷新时看到最新状态。"""
    require_permission(user.role, "envs:read")
    with SessionLocal() as db:
        current_envs = scan_conda_env_dirs()
        sync_current_conda_envs(db, user, current_envs)
        current_paths = {path for _, path in current_envs}
        envs = db.scalars(
            select(Env)
            .where(Env.name != "base")
            .where(or_(Env.path.in_(current_paths), Env.state.in_(tuple(ENV_IMPORT_ACTIVE_STATES | {"error"}))))
            .order_by(Env.name)
        ).all()
        return [mark_env_permissions(user, env_model_to_info(env, db)) for env in envs]


def upload_env_pack(user: UserRecord, payload: EnvUploadRequest) -> EnvInfo:
    """登记用户导入的已有环境，并把真实路径固定到 miniconda 的 envs 目录一级子目录。"""
    require_permission(user.role, "envs:write")
    env_path = build_conda_env_path(payload.name, payload.path)
    with SessionLocal() as db:
        if env_exists(db, payload.name, env_path):
            raise validation_error("environment name already exists")
        model = Env(
            owner_user_id=user.id,
            name=payload.name,
            path=env_path,
            description=payload.description,
            source_type="user_imported",
            state="registered",
            python_version=payload.python_version,
        )
        db.add(model)
        try:
            db.commit()
            db.refresh(model)
        except IntegrityError as exc:
            db.rollback()
            raise validation_error("environment name already exists") from exc
        env = env_model_to_info(model, db)
    record_audit(user.id, "env.upload_pack", "env", str(env.id))
    return mark_env_permissions(user, env)


def import_detected_envs(user: UserRecord) -> list[EnvInfo]:
    """兼容旧按钮语义：执行当前 conda 环境列表扫描并同步数据库。"""
    require_permission(user.role, "envs:read")
    with SessionLocal() as db:
        current_envs = scan_conda_env_dirs()
        sync_current_conda_envs(db, user, current_envs)
        current_paths = {path for _, path in current_envs}
        envs = db.scalars(
            select(Env)
            .where(Env.name != "base")
            .where(or_(Env.path.in_(current_paths), Env.state.in_(tuple(ENV_IMPORT_ACTIVE_STATES | {"error"}))))
            .order_by(Env.name)
        ).all()
        return [mark_env_permissions(user, env_model_to_info(env, db)) for env in envs]


def import_env_archive(user: UserRecord, payload: EnvArchiveImportRequest) -> EnvInfo:
    """创建环境导入记录并启动后台导入，页面通过状态轮询观察导入进度。"""
    require_permission(user.role, "envs:write")
    source = resolve_user_visible_path(normalize_virtual_path(payload.path), user.username, user.role.value)
    if not source.is_file() or source.suffix.lower() != ".zip":
        raise validation_error("environment archive must be a zip file")
    env_name = normalize_env_name(payload.name or source.stem)
    target_path = Path(build_conda_env_path(env_name))
    if target_path.exists():
        raise validation_error("environment name already exists")
    with SessionLocal() as db:
        if env_exists(db, env_name, str(PurePosixPath(target_path.as_posix()))):
            raise validation_error("environment name already exists")
        model = Env(
            owner_user_id=user.id,
            name=env_name,
            path=str(PurePosixPath(target_path.as_posix())),
            description=payload.description or f"从 {payload.path} 导入",
            source_type="user_imported",
            state="importing",
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        saved = mark_env_permissions(user, env_model_to_info(model, db))
    write_env_operation_log(saved, "import", "已创建环境导入任务", user.id, {"archive": payload.path})
    thread = threading.Thread(
        target=run_env_archive_import,
        args=(saved.id, user.id, str(source), str(target_path), payload.path),
        daemon=True,
    )
    thread.start()
    record_audit(user.id, "env.archive.import.start", "env", str(saved.id), detail_json={"archive": payload.path})
    return saved


def clone_env(user: UserRecord, env_id: int, payload: EnvCloneRequest) -> EnvInfo:
    """基于已有环境创建当前用户自己的副本，复制完成后修复路径并检测可用性。"""
    require_permission(user.role, "envs:write")
    source_env = get_env_for_user(user, env_id)
    if source_env.state not in {"available", "registered"}:
        raise validation_error("source environment is not available")
    source_path = resolve_clone_source_env_path(source_env)
    env_name = normalize_env_name(payload.name)
    target_path = Path(build_conda_env_path(env_name))
    if source_path.resolve(strict=False) == target_path.resolve(strict=False):
        raise validation_error("new environment name must be different from source")
    if target_path.exists():
        raise validation_error("environment name already exists")
    target_path_text = str(PurePosixPath(target_path.as_posix()))
    with SessionLocal() as db:
        if env_exists(db, env_name, target_path_text):
            raise validation_error("environment name already exists")
        model = Env(
            owner_user_id=user.id,
            name=env_name,
            path=target_path_text,
            description=payload.description or f"复制自 {source_env.name}",
            source_type="user_clone",
            state="copying",
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        saved = mark_env_permissions(user, env_model_to_info(model, db))
    write_env_operation_log(saved, "clone", "已创建环境副本任务", user.id, {"source_env_id": source_env.id, "source": source_env.path})
    thread = threading.Thread(
        target=run_env_clone,
        args=(saved.id, user.id, str(source_path), str(target_path), source_env.name),
        daemon=True,
    )
    thread.start()
    record_audit(user.id, "env.clone.start", "env", str(saved.id), detail_json={"source_env_id": source_env.id, "source": source_env.path})
    return saved


def test_env(user: UserRecord, env_id: int) -> EnvTestResult:
    """测试任意可用环境，环境使用权限与修改权限分离。"""
    env = get_env_for_user(user, env_id)
    write_env_operation_log(env, "test", "开始检测环境", user.id)
    try:
        result = inspect_env_runtime(env)
    except Exception as exc:
        write_env_operation_log(env, "test", "环境检测异常", user.id, {"error": str(exc)})
        raise
    write_env_operation_log(
        env,
        "test",
        "环境检测通过" if result.ok else "环境检测失败",
        user.id,
        {"error": result.error, "python_version": result.python_version, "package_count": result.package_count},
    )
    return result


def upload_package(
    user: UserRecord,
    env_id: int,
    payload: EnvPackageUploadRequest,
) -> EnvPackageInfo:
    """登记环境包元数据；包安装属于修改环境，必须限制为环境所有者。"""
    env = get_env_for_user(user, env_id)
    require_env_owner(user, env)
    settings = get_settings()
    with SessionLocal() as db:
        model = EnvPackage(
            env_id=env.id,
            owner_user_id=user.id,
            filename=payload.filename,
            package_type=payload.package_type,
            file_path=f"{settings.env_package_root}/{user.username}/{payload.filename}",
            size_bytes=payload.size_bytes,
            sha256=payload.sha256,
            status="uploaded",
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        package = env_package_model_to_info(model)
    write_env_operation_log(env, "package_upload", "已登记待安装包", user.id, {"package": package.filename})
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
    package = load_package_model(package_id)
    if package.env_id != env.id:
        raise validation_error("package does not belong to env")
    command = build_uploaded_package_install_command(package)
    with SessionLocal() as db:
        package_model = db.get(EnvPackage, package.id)
        if package_model is None:
            raise not_found("package not found")
        package_model.status = "queued"
        model = EnvInstallJob(
            package_id=package.id,
            env_id=env.id,
            mode=payload.mode,
            target_node_id=payload.target_node_id,
            visible_gpu_indices=payload.visible_gpu_indices,
            command=command,
            log_path=str(env_operation_log_path(env)),
            created_by=user.id,
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        job = env_install_job_model_to_info(model)
    write_env_operation_log(
        env,
        "package_install",
        "已创建包安装任务",
        user.id,
        {"package_id": package.id, "package": package.filename, "mode": payload.mode},
    )
    record_audit(user.id, "env.package.install", "env_install_job", str(job.id))
    return job


def install_local_package(user: UserRecord, env_id: int, payload: EnvLocalPackageInstallRequest) -> EnvInstallJobInfo:
    """创建本机环境安装作业，由 env_install_worker 后台执行，避免 API 请求长时间占用。"""
    env = get_env_for_user(user, env_id)
    require_env_owner(user, env)
    if not is_env_usable_for_mutation(env):
        raise validation_error("environment is not available")
    method, command, cwd, source_path = build_local_install_job_spec(user, payload)
    log_path = str(env_operation_log_path(env))
    with SessionLocal() as db:
        package = EnvPackage(
            env_id=env.id,
            owner_user_id=user.id,
            filename=Path(source_path).name,
            package_type=method,
            file_path=source_path,
            size_bytes=0,
            sha256="",
            status="queued",
        )
        db.add(package)
        db.flush()
        job = EnvInstallJob(
            package_id=package.id,
            env_id=env.id,
            mode="local",
            command=command,
            workdir=str(cwd) if cwd else "",
            visible_gpu_indices=[],
            status="queued",
            log_path=log_path,
            created_by=user.id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        saved = env_install_job_model_to_info(job)
    write_env_operation_log(env, "package_install", "已创建本机环境安装作业", user.id, {"job_id": saved.id, "method": method, "source": source_path})
    record_audit(user.id, "env.package.install_local.queue", "env_install_job", str(saved.id), detail_json={"method": method})
    return saved


def build_local_install_job_spec(
    user: UserRecord,
    payload: EnvLocalPackageInstallRequest,
) -> tuple[str, str, Path | None, str]:
    """把本机安装请求转换成可持久化的受控命令，worker 重启后仍可继续执行。"""
    if payload.method == "conda":
        package_path = resolve_required_user_file(user, payload.package_path, {".tar.bz2"})
        return "conda", f"conda install --offline -y {shlex.quote(str(package_path))}", None, str(package_path)
    if payload.method == "pip" and payload.pip_mode == "wheel":
        if payload.batch:
            folder = resolve_required_user_directory(user, payload.folder_path)
            requirements = resolve_required_user_file(user, payload.requirements_path, {".txt"})
            command = f"python -m pip install --no-index --find-links={shlex.quote(str(folder))} -r {shlex.quote(str(requirements))}"
            return "pip_batch", command, None, str(requirements)
        package_path = resolve_required_user_file(user, payload.package_path, {".whl"})
        return "pip_wheel", f"python -m pip install --no-index {shlex.quote(str(package_path))}", None, str(package_path)
    if payload.method == "pip" and payload.pip_mode == "folder":
        folder = resolve_required_user_directory(user, payload.folder_path)
        command = "python setup.py install" if payload.folder_command == "setup_py" else "python -m pip install ."
        return "pip_folder", command, folder, str(folder)
    raise validation_error("unsupported install method")


def install_conda_archive_package(user: UserRecord, env: EnvInfo, payload: EnvLocalPackageInstallRequest) -> EnvLocalPackageInstallResult:
    """执行 conda install --offline 安装 .tar.bz2 包。"""
    package_path = resolve_required_user_file(user, payload.package_path, {".tar.bz2"})
    command = f"conda install --offline -y {shlex.quote(str(package_path))}"
    return run_env_install_command(env, "conda", command)


def install_pip_package(user: UserRecord, env: EnvInfo, payload: EnvLocalPackageInstallRequest) -> EnvLocalPackageInstallResult:
    """执行 pip 离线安装，支持单 whl、requirements 批量和文件夹安装。"""
    if payload.pip_mode == "wheel":
        if payload.batch:
            folder = resolve_required_user_directory(user, payload.folder_path)
            requirements = resolve_required_user_file(user, payload.requirements_path, {".txt"})
            command = f"python -m pip install --no-index --find-links={shlex.quote(str(folder))} -r {shlex.quote(str(requirements))}"
            return run_env_install_command(env, "pip_batch", command)
        package_path = resolve_required_user_file(user, payload.package_path, {".whl"})
        command = f"python -m pip install --no-index {shlex.quote(str(package_path))}"
        return run_env_install_command(env, "pip_wheel", command)
    if payload.pip_mode == "folder":
        folder = resolve_required_user_directory(user, payload.folder_path)
        command = "python setup.py install" if payload.folder_command == "setup_py" else "python -m pip install ."
        return run_env_install_command(env, "pip_folder", command, cwd=folder)
    raise validation_error("pip mode is required")


def run_env_install_command(env: EnvInfo, method: str, command: str, cwd: Path | None = None, log_action: str = "package_install_output") -> EnvLocalPackageInstallResult:
    """激活目标环境后执行受控安装命令，并把输出追加到单环境日志。"""
    shell_command = build_conda_shell_command(env, command)
    completed = subprocess.run(["bash", "-lc", shell_command], cwd=str(cwd) if cwd else None, check=False, capture_output=True, text=True, timeout=1800)
    result = EnvLocalPackageInstallResult(
        ok=completed.returncode == 0,
        env_id=env.id,
        env_name=env.name,
        method=method,
        command=command,
        return_code=completed.returncode,
        stdout=tail_text(completed.stdout or "", 12000),
        stderr=tail_text(completed.stderr or "", 12000),
        log_path=str(env_operation_log_path(env)),
    )
    with env_operation_log_path(env).open("a", encoding="utf-8") as file:
        file.write(json.dumps({"time": utc_now(), "env_id": env.id, "env_name": env.name, "action": log_action, "method": method, "command": command, "return_code": completed.returncode, "stdout": completed.stdout or "", "stderr": completed.stderr or ""}, ensure_ascii=False) + "\n")
    persist_env_operation_record(
        env,
        log_action,
        "环境命令执行完成" if completed.returncode == 0 else "环境命令执行失败",
        status="success" if completed.returncode == 0 else "failed",
        command=command,
        return_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        detail={"method": method},
    )
    return result


def build_conda_shell_command(env: EnvInfo, command: str) -> str:
    """复用 conda activate 方式构造环境内命令，保持与检测逻辑一致。"""
    activate_path = Path(get_settings().miniconda_python).parent / "activate"
    return f"source {shlex.quote(str(activate_path))} && conda activate {shlex.quote(env.name)} && {command}"


def resolve_required_user_file(user: UserRecord, path: str | None, allowed_suffixes: set[str]) -> Path:
    """解析用户选择的文件并按后缀校验，防止绕过文件根目录。"""
    if not path:
        raise validation_error("package file is required")
    real_path = resolve_user_visible_path(normalize_virtual_path(path), user.username, user.role.value)
    if not real_path.is_file():
        raise validation_error("selected path is not a file")
    lowered = real_path.name.lower()
    if not any(lowered.endswith(suffix) for suffix in allowed_suffixes):
        raise validation_error("unsupported package file type")
    return real_path


def resolve_required_user_directory(user: UserRecord, path: str | None) -> Path:
    """解析用户选择的目录并确认存在。"""
    if not path:
        raise validation_error("package folder is required")
    real_path = resolve_user_visible_path(normalize_virtual_path(path), user.username, user.role.value)
    if not real_path.is_dir():
        raise validation_error("selected path is not a directory")
    return real_path


def is_env_usable_for_mutation(env: EnvInfo) -> bool:
    """只有可用/登记环境允许执行安装类修改操作。"""
    return env.state in {"available", "registered"}


def tail_text(value: str, limit: int = 4000) -> str:
    """日志 detail 只保留尾部摘要，完整输出已写入单环境日志。"""
    return value[-limit:] if value and len(value) > limit else value


def preview_delete_installed_packages(user: UserRecord, env_id: int, payload: EnvInstalledPackageDeleteRequest) -> EnvInstalledPackageDeletePreview:
    """生成已安装包删除确认内容；执行前必须重新检测包来源和保护状态。"""
    env = get_env_for_user(user, env_id)
    require_env_owner(user, env)
    if not is_env_usable_for_mutation(env):
        raise validation_error("environment is not available")
    packages = resolve_delete_target_packages(env, payload.package_names)
    commands = build_delete_package_commands(packages)
    lines = [
        f"即将从环境 {env.name} 删除以下包：",
        *[f"- {item.name} {item.version}（{package_source_label(item.source)}）" for item in packages],
        "",
        "将执行以下命令：",
        *commands,
        "",
        "系统不会自动修复依赖关系，删除后可能导致依赖该包的程序不可用。是否继续？",
    ]
    return EnvInstalledPackageDeletePreview(env_id=env.id, env_name=env.name, packages=packages, commands=commands, prompt="\n".join(lines))


def delete_installed_packages(user: UserRecord, env_id: int, payload: EnvInstalledPackageDeleteRequest) -> EnvInstalledPackageDeleteResult:
    """删除目标环境内已安装包，并把卸载命令输出写入单环境日志。"""
    env = get_env_for_user(user, env_id)
    require_env_owner(user, env)
    if not is_env_usable_for_mutation(env):
        raise validation_error("environment is not available")
    preview = preview_delete_installed_packages(user, env_id, payload)
    write_env_operation_log(env, "package_delete", "开始删除已安装包", user.id, {"packages": [item.name for item in preview.packages], "commands": preview.commands})
    results = [run_env_install_command(env, "package_delete", command, log_action="package_delete_output") for command in preview.commands]
    ok = all(result.ok for result in results)
    return_code = next((result.return_code for result in results if result.return_code != 0), 0)
    stdout = tail_text("\n".join(result.stdout for result in results if result.stdout), 12000)
    stderr = tail_text("\n".join(result.stderr for result in results if result.stderr), 12000)
    write_env_operation_log(
        env,
        "package_delete",
        "已安装包删除完成" if ok else "已安装包删除失败",
        user.id,
        {"packages": [item.name for item in preview.packages], "return_code": return_code, "stdout": tail_text(stdout), "stderr": tail_text(stderr)},
    )
    record_audit(user.id, "env.package.delete_installed", "env", str(env.id), result="success" if ok else "failed", detail_json={"packages": [item.name for item in preview.packages], "return_code": return_code})
    return EnvInstalledPackageDeleteResult(
        ok=ok,
        env_id=env.id,
        env_name=env.name,
        packages=preview.packages,
        commands=preview.commands,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        log_path=str(env_operation_log_path(env)),
    )


def resolve_delete_target_packages(env: EnvInfo, package_names: list[str]) -> list[EnvPackageVersion]:
    """根据当前环境真实包列表解析待删除包，并拒绝核心包和不存在的包。"""
    requested = {normalize_package_name(name): name for name in package_names if name and name.strip()}
    if not requested:
        raise validation_error("package name is required")
    runtime = inspect_env_runtime(env)
    by_name = {normalize_package_name(item.name): item for item in runtime.packages}
    missing = [original for key, original in requested.items() if key not in by_name]
    if missing:
        raise validation_error("package not found", data={"packages": missing})
    packages = [by_name[key] for key in requested]
    protected = [item.name for item in packages if item.protected or is_protected_package_name(item.name)]
    if protected:
        raise validation_error("protected package cannot be deleted", data={"packages": protected})
    return sorted(packages, key=lambda item: item.name.lower())


def build_delete_package_commands(packages: list[EnvPackageVersion]) -> list[str]:
    """按包来源分组生成卸载命令，conda/pip 包分别使用对应工具。"""
    conda_names = [item.name for item in packages if item.source != "pip"]
    pip_names = [item.name for item in packages if item.source == "pip"]
    commands = []
    if conda_names:
        commands.append("conda remove -y " + " ".join(shlex.quote(name) for name in conda_names))
    if pip_names:
        commands.append("python -m pip uninstall -y " + " ".join(shlex.quote(name) for name in pip_names))
    return commands


def normalize_package_name(name: str) -> str:
    """归一化包名，兼容 Python 包名中横线、下划线和点号的等价写法。"""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def is_protected_package_name(name: str) -> bool:
    """判断是否为环境基础包；该判断与远端探测脚本保持一致但以后端为准。"""
    return normalize_package_name(name) in PROTECTED_ENV_PACKAGE_NAMES


def package_source_label(source: str) -> str:
    """把包来源转换为中文确认文案。"""
    return "pip" if source == "pip" else "conda"


def delete_package(user: UserRecord, env_id: int, package_id: int) -> EnvPackageInfo:
    """删除环境包登记记录并写入环境日志；真实卸载流程接入后继续复用该日志入口。"""
    env = get_env_for_user(user, env_id)
    require_env_owner(user, env)
    package_model = load_package_model(package_id)
    package = env_package_model_to_info(package_model)
    if package.env_id != env.id:
        raise validation_error("package does not belong to env")
    with SessionLocal() as db:
        model = db.get(EnvPackage, package_id)
        if model is None:
            raise not_found("package not found")
        db.delete(model)
        db.commit()
    write_env_operation_log(env, "package_delete", "已删除环境包记录", user.id, {"package_id": package.id, "package": package.filename})
    record_audit(user.id, "env.package.delete", "env_package", str(package.id))
    return package


def get_install_job(user: UserRecord, job_id: int) -> EnvInstallJobInfo:
    """返回环境安装作业详情，并复用环境使用权限判断。"""
    job = require_job(job_id)
    get_env_for_user(user, job.env_id)
    return job


def get_install_job_log(user: UserRecord, job_id: int) -> str:
    """返回环境安装作业关联的环境日志文件内容，日志文件缺失时给出空日志提示。"""
    job = get_install_job(user, job_id)
    log_path = Path(job.log_path)
    if not log_path.is_file():
        return f"[env-install-job-{job.id}] status={job.status}\nlog file not found: {job.log_path}\n"
    return log_path.read_text(encoding="utf-8", errors="replace")


def get_env_operation_log(user: UserRecord, env_id: int) -> str:
    """读取单个环境的落盘操作日志；管理员可看全部，普通用户只可看自己的环境。"""
    env = get_env_for_user(user, env_id)
    require_env_log_permission(user, env)
    log_path = env_operation_log_path(env)
    if not log_path.is_file():
        return f"[env-{env.id}] log file not found: {log_path}\n"
    return log_path.read_text(encoding="utf-8", errors="replace")


def cancel_install_job(user: UserRecord, job_id: int) -> EnvInstallJobInfo:
    """取消自己创建的环境安装作业，并记录审计信息。"""
    job = get_install_job(user, job_id)
    if job.created_by != user.id:
        raise forbidden("job creator required")
    if job.status in {"succeeded", "failed", "cancelled"}:
        raise validation_error("finished job cannot be cancelled")
    with SessionLocal() as db:
        model = db.get(EnvInstallJob, job_id)
        if model is None:
            raise not_found("install job not found")
        model.status = "cancelled"
        model.finished_at = local_datetime()
        db.commit()
        db.refresh(model)
        job = env_install_job_model_to_info(model)
    env = get_env_for_user(user, job.env_id)
    write_env_operation_log(env, "package_install", "已取消包安装任务", user.id, {"job_id": job.id, "package_id": job.package_id})
    record_audit(user.id, "env.install.cancel", "env_install_job", str(job.id))
    return job


def delete_env(user: UserRecord, env_id: int) -> EnvInfo:
    """删除环境元数据和 conda envs 下的对应目录；权限由管理员/所有者规则控制。"""
    env = get_env_for_user(user, env_id)
    require_env_delete_permission(user, env)
    if env.state in ENV_IMPORT_ACTIVE_STATES:
        raise validation_error("environment is busy")
    target_path = resolve_deletable_env_path(env)
    write_env_operation_log(env, "delete", "开始删除环境", user.id, {"path": env.path})
    if target_path.exists():
        try:
            shutil.rmtree(target_path)
        except OSError as exc:
            write_env_operation_log(env, "delete", "环境目录删除失败", user.id, {"path": env.path, "error": str(exc)})
            raise validation_error("environment directory delete failed", data={"path": env.path, "error": str(exc)}) from exc
        write_env_operation_log(env, "delete", "环境目录已删除", user.id, {"path": env.path})
    with SessionLocal() as db:
        model = db.get(Env, env_id)
        if model is None:
            raise not_found("env not found")
        # 删除环境前清理依赖记录，避免任务历史或包安装记录的外键阻止环境下线。
        db.execute(update(Task).where(Task.env_id == env_id).values(env_id=None))
        db.execute(delete(EnvPackageManifest).where(EnvPackageManifest.env_id == env_id))
        db.execute(delete(EnvInstallJob).where(EnvInstallJob.env_id == env_id))
        db.execute(delete(EnvPackage).where(EnvPackage.env_id == env_id))
        db.delete(model)
        db.commit()
    _PACKAGES[:] = [package for package in _PACKAGES if package.env_id != env.id]
    _JOBS[:] = [job for job in _JOBS if job.env_id != env.id]
    write_env_operation_log(env, "delete", "环境数据库记录已删除", user.id)
    record_audit(user.id, "env.delete", "env", str(env.id), detail_json={"path": env.path})
    return env


def get_env_for_user(user: UserRecord, env_id: int) -> EnvInfo:
    """获取用户可使用的环境；所有具备 envs:read 的用户都可使用全部环境。"""
    require_permission(user.role, "envs:read")
    with SessionLocal() as db:
        env = db.get(Env, env_id)
        if env is None or env.name == "base":
            raise not_found("env not found")
        return mark_env_permissions(user, env_model_to_info(env, db))


def require_env_owner(user: UserRecord, env: EnvInfo) -> None:
    """断言用户可以修改该环境；管理员可修改全部环境，普通用户只修改自己的环境。"""
    if user.role == Role.ADMIN:
        return
    if env.source_type == "system_imported":
        raise forbidden("admin required for system env")
    if env.owner_user_id != user.id:
        raise forbidden("env owner required")


def require_env_delete_permission(user: UserRecord, env: EnvInfo) -> None:
    """校验环境删除权限：管理员可删全部环境，普通用户仅可删自己的导入环境。"""
    if user.role == Role.ADMIN:
        return
    if env.source_type == "system_imported":
        raise forbidden("admin required for system env")
    require_env_owner(user, env)


def require_env_log_permission(user: UserRecord, env: EnvInfo) -> None:
    """校验环境日志读取权限：管理员可查全部，普通用户只可查自己拥有的环境。"""
    if user.role == Role.ADMIN:
        return
    require_env_owner(user, env)


def resolve_deletable_env_path(env: EnvInfo) -> Path:
    """只允许删除 conda envs 根目录下的一级环境目录，避免异常路径导致误删。"""
    root = Path(get_settings().conda_env_root).resolve(strict=False)
    target = Path(env.path)
    resolved = target.resolve(strict=False)
    if env.name == "base" or resolved == root or resolved.parent != root:
        raise validation_error("environment path is outside conda env root")
    if target.is_symlink():
        raise validation_error("environment path must not be a symlink")
    if target.exists() and not target.is_dir():
        raise validation_error("environment path is not a directory")
    return target


def resolve_clone_source_env_path(env: EnvInfo) -> Path:
    """校验副本来源必须是 conda envs 根目录下真实存在的一级环境目录。"""
    root = Path(get_settings().conda_env_root).resolve(strict=False)
    target = Path(env.path)
    resolved = target.resolve(strict=False)
    if env.name == "base" or resolved == root or resolved.parent != root:
        raise validation_error("source environment path is outside conda env root")
    if target.is_symlink():
        raise validation_error("source environment path must not be a symlink")
    if not target.is_dir():
        raise validation_error("source environment directory not found")
    return target


def load_package_model(package_id: int) -> EnvPackage:
    """按 ID 从数据库获取环境包元数据，避免进程重启后内存队列丢失。"""
    with SessionLocal() as db:
        package = db.get(EnvPackage, package_id)
        if package is None:
            raise not_found("package not found")
        db.expunge(package)
        return package


def require_package(package_id: int) -> EnvPackageInfo:
    """按 ID 获取环境包元数据，找不到时抛出 NOT_FOUND。"""
    return env_package_model_to_info(load_package_model(package_id))


def require_job(job_id: int) -> EnvInstallJobInfo:
    """按 ID 获取环境安装作业，找不到时抛出 NOT_FOUND。"""
    with SessionLocal() as db:
        job = db.get(EnvInstallJob, job_id)
        if job is None:
            raise not_found("install job not found")
        return env_install_job_model_to_info(job)


def build_uploaded_package_install_command(package: EnvPackage) -> str:
    """把已登记包转换为 worker 可执行命令；compile 模式后续可在远端复用同一字段。"""
    package_path = shlex.quote(package.file_path)
    if package.package_type in {"conda", "conda_archive", "tar_bz2"} or package.file_path.endswith(".tar.bz2"):
        return f"conda install --offline -y {package_path}"
    return f"python -m pip install {package_path}"


def env_package_model_to_info(package: EnvPackage) -> EnvPackageInfo:
    """把数据库包记录转换为 API 响应模型，避免服务层泄漏 ORM 对象。"""
    created_at = ensure_local_datetime(package.created_at)
    return EnvPackageInfo(
        id=package.id,
        env_id=package.env_id,
        owner_user_id=package.owner_user_id,
        filename=package.filename,
        package_type=package.package_type,
        file_path=package.file_path,
        size_bytes=package.size_bytes,
        sha256=package.sha256,
        status=package.status,
        created_at=created_at.isoformat() if created_at else utc_now(),
    )


def env_install_job_model_to_info(job: EnvInstallJob) -> EnvInstallJobInfo:
    """把数据库安装作业转换为前端需要的稳定字段。"""
    started_at = ensure_local_datetime(job.started_at)
    finished_at = ensure_local_datetime(job.finished_at)
    return EnvInstallJobInfo(
        id=job.id,
        package_id=job.package_id,
        env_id=job.env_id,
        mode=job.mode,
        target_node_id=job.target_node_id,
        visible_gpu_indices=job.visible_gpu_indices or [],
        status=job.status,
        remote_pid=job.remote_pid,
        log_path=job.log_path,
        return_code=job.return_code,
        created_by=job.created_by,
        started_at=started_at.isoformat() if started_at else None,
        finished_at=finished_at.isoformat() if finished_at else None,
    )


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


def sync_current_conda_envs(db, user: UserRecord, current_envs: list[tuple[str, str]]) -> list[Env]:
    """执行 env list 同步：扫描 envs 目录，把新增环境作为系统环境落库，已有环境保持原来源。"""
    detected: list[Env] = []
    existing = {(env.name, env.path) for env in db.scalars(select(Env)).all()}
    for name, path in current_envs:
        if (name, path) in existing:
            continue
        model = Env(
            owner_user_id=user.id,
            name=name,
            path=path,
            description="env list 自动发现的 conda 环境",
            source_type="system_imported",
            state="available",
        )
        db.add(model)
        detected.append(model)
    if detected:
        db.commit()
        for model in detected:
            db.refresh(model)
    return detected


def run_env_archive_import(env_id: int, actor_user_id: int, source_path: str, target_env_path: str, virtual_source: str) -> None:
    """后台执行环境导入状态机：导入中、修复中、测试中、可用或错误。"""
    runtime_root = Path(get_settings().runtime_root) / "env_import" / uuid4().hex
    extract_root = runtime_root / "extract"
    target_path = Path(target_env_path)
    copied_target = False
    try:
        update_env_import_state(env_id, "importing")
        write_env_operation_log_by_id(env_id, "import", "开始解压环境包", actor_user_id, {"archive": virtual_source})
        extract_root.mkdir(parents=True, exist_ok=False)
        safe_extract_env_zip(Path(source_path), extract_root)
        write_env_operation_log_by_id(env_id, "import", "环境包解压完成", actor_user_id)
        unpacked_env = find_unpacked_env_root(extract_root)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            raise validation_error("environment target already exists")
        shutil.copytree(unpacked_env, target_path)
        copied_target = True
        write_env_operation_log_by_id(env_id, "import", "环境文件已复制到目标目录", actor_user_id, {"target": str(target_path)})

        update_env_import_state(env_id, "fixing")
        write_env_operation_log_by_id(env_id, "fix", "开始修复环境路径", actor_user_id)
        fix_imported_env_paths(target_path, target_path)
        write_env_operation_log_by_id(env_id, "fix", "环境路径修复完成", actor_user_id)
        apply_imported_env_permissions(target_path)
        write_env_operation_log_by_id(env_id, "fix", "环境归属和权限修复完成", actor_user_id)

        update_env_import_state(env_id, "testing")
        write_env_operation_log_by_id(env_id, "test", "开始导入后环境检测", actor_user_id)
        env = load_env_info_for_background(env_id)
        result = inspect_env_runtime(env)
        if not result.ok:
            write_env_operation_log_by_id(env_id, "test", "导入后环境检测失败", actor_user_id, {"error": result.error})
            raise validation_error("environment test failed", data={"error": result.error})
        write_env_operation_log_by_id(
            env_id,
            "test",
            "导入后环境检测通过",
            actor_user_id,
            {"python_version": result.python_version, "package_count": result.package_count},
        )

        with SessionLocal() as db:
            model = db.get(Env, env_id)
            if model is not None:
                model.state = "available"
                model.python_version = result.python_version
                db.commit()
        write_env_operation_log_by_id(env_id, "import", "环境导入完成，状态已设为可用", actor_user_id)
        record_audit(actor_user_id, "env.archive.import.finish", "env", str(env_id), detail_json={"archive": virtual_source})
    except Exception as exc:
        if copied_target and target_path.exists():
            shutil.rmtree(target_path, ignore_errors=True)
        try_write_env_operation_log_by_id(env_id, "import", "环境导入失败", actor_user_id, {"archive": virtual_source, "error": str(exc)})
        update_env_import_state(env_id, "error", str(exc))
        record_audit(actor_user_id, "env.archive.import.fail", "env", str(env_id), result="failed", detail_json={"archive": virtual_source, "error": str(exc)})
    finally:
        if runtime_root.exists():
            shutil.rmtree(runtime_root, ignore_errors=True)


def run_env_clone(env_id: int, actor_user_id: int, source_env_path: str, target_env_path: str, source_env_name: str) -> None:
    """后台执行环境副本状态机：复制中、修复中、测试中、可用或错误。"""
    source_path = Path(source_env_path)
    target_path = Path(target_env_path)
    copied_target = False
    try:
        update_env_import_state(env_id, "copying")
        write_env_operation_log_by_id(env_id, "clone", "开始复制环境目录", actor_user_id, {"source": str(source_path), "target": str(target_path)})
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            raise validation_error("environment target already exists")
        shutil.copytree(source_path, target_path)
        copied_target = True
        write_env_operation_log_by_id(env_id, "clone", "环境目录复制完成", actor_user_id)

        update_env_import_state(env_id, "fixing")
        write_env_operation_log_by_id(env_id, "fix", "开始修复副本环境路径", actor_user_id)
        fix_imported_env_paths(target_path, target_path, {str(PurePosixPath(source_path.as_posix()))})
        write_env_operation_log_by_id(env_id, "fix", "副本环境路径修复完成", actor_user_id)
        apply_imported_env_permissions(target_path)
        write_env_operation_log_by_id(env_id, "fix", "副本环境归属和权限修复完成", actor_user_id)

        update_env_import_state(env_id, "testing")
        write_env_operation_log_by_id(env_id, "test", "开始副本环境检测", actor_user_id)
        env = load_env_info_for_background(env_id)
        result = inspect_env_runtime(env)
        if not result.ok:
            write_env_operation_log_by_id(env_id, "test", "副本环境检测失败", actor_user_id, {"error": result.error})
            raise validation_error("environment test failed", data={"error": result.error})
        write_env_operation_log_by_id(
            env_id,
            "test",
            "副本环境检测通过",
            actor_user_id,
            {"python_version": result.python_version, "package_count": result.package_count},
        )

        with SessionLocal() as db:
            model = db.get(Env, env_id)
            if model is not None:
                model.state = "available"
                model.python_version = result.python_version
                db.commit()
        write_env_operation_log_by_id(env_id, "clone", "环境副本创建完成，状态已设为可用", actor_user_id)
        record_audit(actor_user_id, "env.clone.finish", "env", str(env_id), detail_json={"source": source_env_path, "source_name": source_env_name})
    except Exception as exc:
        if copied_target and target_path.exists():
            shutil.rmtree(target_path, ignore_errors=True)
        try_write_env_operation_log_by_id(env_id, "clone", "环境副本创建失败", actor_user_id, {"source": source_env_path, "error": str(exc)})
        update_env_import_state(env_id, "error", str(exc))
        record_audit(actor_user_id, "env.clone.fail", "env", str(env_id), result="failed", detail_json={"source": source_env_path, "error": str(exc)})


def update_env_import_state(env_id: int, state: str, message: str | None = None) -> None:
    """更新环境后台处理状态；错误信息写入 description，便于页面无需新增字段即可看到失败原因。"""
    with SessionLocal() as db:
        model = db.get(Env, env_id)
        if model is None:
            return
        model.state = state
        if message:
            model.description = f"{model.description}\n处理失败：{message}"[:512]
        db.commit()


def write_env_operation_log_by_id(env_id: int, action: str, message: str, actor_user_id: int | None = None, detail: dict | None = None) -> Path:
    """按环境 ID 重新加载环境并写日志，供后台线程避免跨线程复用 ORM 对象。"""
    env = load_env_info_for_background(env_id)
    return write_env_operation_log(env, action, message, actor_user_id, detail)


def try_write_env_operation_log_by_id(env_id: int, action: str, message: str, actor_user_id: int | None = None, detail: dict | None = None) -> None:
    """失败处理路径只尽力记录日志，避免日志异常覆盖真正的导入失败原因。"""
    try:
        write_env_operation_log_by_id(env_id, action, message, actor_user_id, detail)
    except Exception:
        return


def write_env_operation_log(env: EnvInfo, action: str, message: str, actor_user_id: int | None = None, detail: dict | None = None) -> Path:
    """把环境操作追加写入单环境日志文件，保证导入、修复、测试和包操作都有落盘记录。"""
    path = env_operation_log_path(env)
    entry = {
        "time": utc_now(),
        "env_id": env.id,
        "env_name": env.name,
        "action": action,
        "message": message,
        "actor_user_id": actor_user_id,
        "detail": detail or {},
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    persist_env_operation_record(env, action, message, actor_user_id, detail or {}, log_path=str(path))
    return path


def persist_env_operation_record(
    env: EnvInfo,
    action: str,
    message: str,
    actor_user_id: int | None = None,
    detail: dict | None = None,
    status: str = "info",
    command: str | None = None,
    return_code: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    log_path: str | None = None,
) -> None:
    """把环境操作同步写入数据库；落库失败不应覆盖真实环境操作结果。"""
    try:
        with SessionLocal() as db:
            db.add(
                EnvOperationLog(
                    env_id=env.id,
                    env_name=env.name,
                    action=action,
                    message=message,
                    actor_user_id=actor_user_id,
                    status=status,
                    command=command,
                    return_code=return_code,
                    stdout=tail_text(stdout or "", 12000) if stdout is not None else None,
                    stderr=tail_text(stderr or "", 12000) if stderr is not None else None,
                    detail_json=detail or {},
                    log_path=log_path or str(env_operation_log_path(env)),
                )
            )
            db.commit()
    except Exception:
        return


def env_operation_log_path(env: EnvInfo) -> Path:
    """按环境生成固定日志路径；文件名包含 ID，避免重名环境历史日志互相覆盖。"""
    root = Path(get_settings().env_install_log_root)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"env-{env.id}-{safe_log_filename(env.name)}.log"


def safe_log_filename(name: str) -> str:
    """把环境名压成安全文件名，防止特殊字符逃逸日志目录。"""
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in name.strip())
    return (cleaned or "env")[:80]


def load_env_info_for_background(env_id: int) -> EnvInfo:
    """后台线程重新从数据库读取环境信息，避免跨线程复用 ORM 对象。"""
    with SessionLocal() as db:
        model = db.get(Env, env_id)
        if model is None:
            raise not_found("env not found")
        return env_model_to_info(model, db)


def safe_extract_env_zip(source: Path, target: Path) -> None:
    """安全解压用户上传的环境 zip，拒绝绝对路径、上级目录和软链接成员。"""
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            pure = PurePosixPath(member.filename)
            if pure.is_absolute() or ".." in pure.parts:
                raise validation_error("archive contains unsafe path")
            mode = member.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                raise validation_error("archive links are not allowed")
            destination = (target / member.filename).resolve(strict=False)
            resolved_target = target.resolve(strict=False)
            if destination != resolved_target and resolved_target not in destination.parents:
                raise validation_error("archive escapes target directory")
        archive.extractall(target)


def find_unpacked_env_root(extract_root: Path) -> Path:
    """在解压目录中寻找真正的环境根目录，兼容 zip 外层多包一层目录的情况。"""
    candidates = [path for path in [extract_root, *extract_root.rglob("*")] if path.is_dir()]
    for candidate in candidates:
        if (candidate / "bin" / "python").is_file() or (candidate / "Scripts" / "python.exe").is_file():
            return candidate
    raise validation_error("environment python executable not found in archive")


def fix_imported_env_paths(env_root: Path, target_path: Path, extra_old_prefixes: set[str] | None = None) -> None:
    """对环境中的文本文件做旧前缀替换，覆盖 pip、包配置和 conda 元数据等路径。"""
    old_prefixes = detect_old_prefixes(env_root) | (extra_old_prefixes or set())
    if not old_prefixes:
        return
    target = str(PurePosixPath(target_path.as_posix()))
    encoded_replacements = [(old.encode("utf-8"), target.encode("utf-8")) for old in sorted(old_prefixes, key=len, reverse=True) if old and old != target]
    for path in env_root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            continue
        raw = path.read_bytes()
        if not is_probably_text_bytes(raw):
            continue
        replaced = raw
        for old, new in encoded_replacements:
            replaced = replaced.replace(old, new)
        if replaced != raw:
            path.write_bytes(replaced)


def is_probably_text_bytes(raw: bytes) -> bool:
    """用保守规则识别文本文件：含空字节视为二进制，避免误改 so/pyd 等二进制包。"""
    sample = raw[:4096]
    if b"\x00" in sample:
        return False
    if not sample:
        return True
    control = sum(1 for byte in sample if byte < 32 and byte not in {9, 10, 12, 13})
    return control / len(sample) < 0.05


def apply_imported_env_permissions(env_root: Path) -> None:
    """修正导入环境的文件归属和权限，避免解压来源导致后续测试或包管理不可用。"""
    owner = resolve_imported_env_owner()
    paths = [env_root, *env_root.rglob("*")]
    for path in paths:
        if path.is_symlink():
            continue
        if owner is not None:
            os.chown(path, owner[0], owner[1])
        if path.is_dir():
            path.chmod(0o755)
            continue
        if path.is_file():
            path.chmod(0o755 if should_mark_env_file_executable(path) else 0o644)


def resolve_imported_env_owner() -> tuple[int, int] | None:
    """在 Linux 且当前进程有权限时，将环境归属设置为配置中的主运行用户。"""
    if os.name != "posix" or not hasattr(os, "geteuid"):
        return None
    try:
        import pwd
    except ImportError:
        return None
    try:
        user = pwd.getpwnam(get_settings().main_linux_user)
    except KeyError:
        return None
    current_uid = os.geteuid()
    if current_uid == 0:
        return user.pw_uid, user.pw_gid
    if current_uid == user.pw_uid:
        return None
    return None


def should_mark_env_file_executable(path: Path) -> bool:
    """保留或补齐环境脚本的执行权限，防止 zip 打包来源丢失 bin/Scripts 入口权限。"""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if mode & 0o111:
        return True
    return path.parent.name in {"bin", "Scripts"}


def detect_old_prefixes(env_root: Path) -> set[str]:
    """扫描环境内所有文本文件，提取包含环境名的旧前缀用于修复 pip 和包配置路径。"""
    prefixes: set[str] = set()
    current_prefix = str(PurePosixPath(env_root.as_posix()))
    for path in env_root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not is_probably_text_bytes(raw):
            continue
        text = raw.decode("utf-8", errors="ignore")
        prefixes.update(extract_old_prefixes_from_text(text, env_root.name))
    return {item for item in prefixes if len(item) >= 4 and item != current_prefix}


def extract_old_prefixes_from_text(text: str, env_name: str) -> set[str]:
    """从任意文本中提取旧环境根路径，兼容 shebang、pip 脚本、conda metadata 和包配置。"""
    prefixes: set[str] = set()
    for line in text.splitlines():
        if line.startswith("# prefix:"):
            candidate = line.split(":", 1)[1].strip()
            normalized = normalize_old_prefix_candidate(candidate, env_name)
            if normalized:
                prefixes.add(normalized)
    for candidate in re.findall(r"(?:/[^\s'\"`<>|;&]+|[A-Za-z]:[/\\][^\s'\"`<>|;&]+)", text):
        normalized = normalize_old_prefix_candidate(candidate, env_name)
        if normalized:
            prefixes.add(normalized)
    return prefixes


def normalize_old_prefix_candidate(candidate: str, env_name: str) -> str | None:
    """把旧解释器路径或包内路径收敛到环境根目录，避免把 /bin/python 误当成前缀。"""
    cleaned = candidate.strip().rstrip("),.;:]}")
    variants = [f"/envs/{env_name}", f"\\envs\\{env_name}"]
    for marker in variants:
        index = cleaned.find(marker)
        if index >= 0:
            return cleaned[: index + len(marker)].replace("\\", "/")
    for marker in (f"/{env_name}/bin/", f"/{env_name}/lib/", f"/{env_name}/include/", f"/{env_name}/conda-meta/"):
        index = cleaned.find(marker)
        if index >= 0:
            return cleaned[: index + len(f"/{env_name}")].replace("\\", "/")
    marker = f"\\{env_name}\\Scripts\\"
    index = cleaned.find(marker)
    if index >= 0:
        return cleaned[: index + len(f"\\{env_name}")].replace("\\", "/")
    return None


def normalize_env_name(name: str) -> str:
    """复用环境名限制，确保 zip 导入不会创建嵌套目录或覆盖特殊路径。"""
    build_conda_env_path(name)
    return name.strip()


def scan_conda_env_dirs() -> list[tuple[str, str]]:
    """优先执行 conda env list 获取环境；命令不可用时回退扫描配置的 envs 目录。"""
    listed = scan_conda_envs_by_command()
    if listed:
        return listed
    root = Path(get_settings().conda_env_root)
    if not root.is_dir():
        return []
    envs: list[tuple[str, str]] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir() or child.name == "base":
            continue
        envs.append((child.name, str(PurePosixPath(child.as_posix()))))
    return envs


def scan_conda_envs_by_command() -> list[tuple[str, str]]:
    """通过 conda env list --json 获取当前环境，避免只按目录猜测 conda 是否可识别。"""
    settings = get_settings()
    try:
        if os.name == "nt":
            conda_bat = Path(settings.conda_env_root).parent / "Library" / "bin" / "conda.bat"
            command = ["cmd", "/d", "/s", "/c", f'call "{conda_bat}" env list --json']
        else:
            activate_path = Path(settings.miniconda_python).parent / "activate"
            command = ["bash", "-lc", f"source {shlex.quote(str(activate_path))} && conda env list --json"]
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)
        payload = json.loads(completed.stdout)
    except Exception:
        return []
    envs: list[tuple[str, str]] = []
    configured_root = Path(settings.conda_env_root).resolve(strict=False)
    for raw_path in payload.get("envs", []):
        path = Path(raw_path).resolve(strict=False)
        name = path.name
        if name == "base" or path == configured_root.parent:
            continue
        if path.parent != configured_root:
            continue
        envs.append((name, str(PurePosixPath(path.as_posix()))))
    return sorted(envs, key=lambda item: item[0].lower())


def env_exists(db, name: str, path: str) -> bool:
    """按环境名或路径判断是否已入库，避免重复导入同一 conda 环境。"""
    return db.scalar(select(Env).where((Env.name == name) | (Env.path == path))) is not None


def env_model_to_info(env: Env, db=None) -> EnvInfo:
    """把数据库环境模型转换成前端响应模型，避免服务层直接暴露 ORM 对象。"""
    return EnvInfo(
        id=env.id,
        owner_user_id=env.owner_user_id,
        owner_name=env_owner_display_name(db, env) if db is not None else None,
        name=env.name,
        path=env.path,
        can_modify=False,
        description=env.description,
        source_type=env.source_type,
        state=env.state,
        python_version=env.python_version,
        size_bytes=env.size_bytes,
        created_at=ensure_local_datetime(env.created_at).isoformat() if env.created_at else utc_now(),
    )


def env_owner_display_name(db, env: Env) -> str | None:
    """仅为普通用户导入的环境返回所有人姓名；系统环境和管理员环境保持空白。"""
    if env.source_type == "system_imported":
        return None
    owner = db.get(User, env.owner_user_id)
    if owner is None or owner.role == Role.ADMIN.value:
        return None
    return owner.real_name or owner.username


def inspect_env_runtime(env: EnvInfo) -> EnvTestResult:
    """先激活 conda 环境再采集信息，确保 CUDA、库路径和包解析与真实任务一致。"""
    command = build_conda_probe_command(env)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return empty_env_test_result(env, str(exc))
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or f"python exited with {completed.returncode}").strip()
        return empty_env_test_result(env, message)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return empty_env_test_result(env, f"invalid probe output: {exc}")
    packages = payload.get("packages", [])
    return EnvTestResult(
        ok=True,
        env_id=env.id,
        env_name=env.name,
        env_path=env.path,
        python_executable=payload.get("python_executable"),
        python_version=payload.get("python_version"),
        pytorch=EnvFrameworkInfo(**payload.get("pytorch", {"installed": False})),
        tensorflow=EnvFrameworkInfo(**payload.get("tensorflow", {"installed": False})),
        packages=[EnvPackageVersion(**item) for item in packages],
        package_count=int(payload.get("package_count") or len(packages)),
    )


def build_conda_probe_command(env: EnvInfo) -> list[str]:
    """构造受控的 conda activate 检测命令；探针脚本放在 remote 目录，部署时随远端脚本同步。"""
    probe_path = env_probe_script_path()
    activate_path = Path(get_settings().miniconda_python).parent / "activate"
    shell_command = (
        f"source {shlex.quote(str(activate_path))}"
        f" && conda activate {shlex.quote(env.name)}"
        f" && python -u {shlex.quote(str(probe_path))}"
    )
    return ["bash", "-lc", shell_command]


def env_probe_script_path() -> Path:
    """优先使用已同步到 remote_code_root 的探针脚本，本地开发时回退到仓库内 remote 文件。"""
    deployed_path = Path(get_settings().remote_code_root) / "env_probe.py"
    if deployed_path.is_file():
        return deployed_path
    return Path(__file__).resolve().parents[1] / "remote" / "env_probe.py"


def empty_env_test_result(env: EnvInfo, error: str, python_executable: str | None = None) -> EnvTestResult:
    """构造失败检测结果，保持响应结构稳定，避免前端为异常路径写特殊分支。"""
    missing = EnvFrameworkInfo(installed=False)
    return EnvTestResult(
        ok=False,
        env_id=env.id,
        env_name=env.name,
        env_path=env.path,
        python_executable=python_executable,
        pytorch=missing,
        tensorflow=missing,
        error=error,
    )


def mark_env_permissions(user: UserRecord, env: EnvInfo) -> EnvInfo:
    """返回带当前用户修改权限标记的副本，避免把权限状态写回全局环境元数据。"""
    data = env.model_dump()
    data["can_modify"] = user.role == Role.ADMIN or (env.source_type != "system_imported" and env.owner_user_id == user.id)
    return EnvInfo(**data)
