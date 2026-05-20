import base64
import mimetypes
import shutil
import stat
import subprocess
import tarfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.errors import not_found, validation_error
from app.core.path_resolver import normalize_virtual_path, resolve_user_visible_path
from app.core.rbac import require_permission
from app.core.time_utils import ensure_local_datetime, local_datetime
from app.db.models import FileJob as FileJobModel
from app.db.session import SessionLocal
from app.schemas.files import FileEntry, FileJobData, FileListData, FilePreviewData
from app.services.audit_service import record_audit
from app.services.auth_service import UserRecord

TEXT_PREVIEW_LIMIT = 256 * 1024
BINARY_PREVIEW_LIMIT = 2 * 1024 * 1024
ACTIVE_JOB_STATES = ("pending", "running")
ARCHIVE_SUFFIXES = {".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"}
MAX_ACTIVE_FILE_JOBS = 4
EDITABLE_TEXT_TYPES = {
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".env",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}


def list_files(user: UserRecord, path: str) -> FileListData:
    """列出用户可见目录，路径解析统一经过 PathResolver。"""
    require_permission(user.role, "files:read")
    normalized = normalize_virtual_path(path)
    real_path = resolve_user_visible_path(normalized, user.username, user.role.value)
    if not real_path.exists():
        return FileListData(path=normalized, items=[])
    if not real_path.is_dir():
        raise validation_error("path is not a directory")
    items = [build_file_entry(child, normalized) for child in sorted(real_path.iterdir(), key=file_sort_key)]
    return FileListData(path=normalized, items=items)


def preview_file(user: UserRecord, path: str) -> FilePreviewData:
    """按文件类型返回预览内容，大文件只返回前段内容避免拖垮页面。"""
    require_permission(user.role, "files:read")
    normalized = normalize_virtual_path(path)
    real_path = resolve_user_visible_path(normalized, user.username, user.role.value)
    if not real_path.exists() or not real_path.is_file():
        raise validation_error("path is not a file")

    size = real_path.stat().st_size
    content_type = guess_content_type(real_path)
    if is_text_file(real_path, content_type):
        raw = real_path.read_bytes()[:TEXT_PREVIEW_LIMIT]
        content = raw.decode("utf-8", errors="replace")
        return FilePreviewData(
            path=normalized,
            content_type=content_type,
            content=content,
            encoding="text",
            truncated=size > TEXT_PREVIEW_LIMIT,
            size_bytes=size,
        )

    raw = real_path.read_bytes()[:BINARY_PREVIEW_LIMIT]
    return FilePreviewData(
        path=normalized,
        content_type=content_type,
        content=base64.b64encode(raw).decode("ascii"),
        encoding="base64",
        truncated=size > BINARY_PREVIEW_LIMIT,
        size_bytes=size,
    )


def create_directory(user: UserRecord, path: str) -> dict[str, str | bool]:
    """创建目录时只允许在用户可见根内落盘，已存在路径会被拒绝。"""
    real_path = resolve_writable_path(user, path)
    if real_path.exists():
        raise validation_error("target already exists")
    ensure_parent_directory(real_path)
    real_path.mkdir(parents=False)
    record_file_audit(user, "mkdir", path)
    return {"accepted": True, "action": "mkdir", "path": normalize_virtual_path(path)}


def create_text_file(user: UserRecord, path: str, content: str = "") -> dict[str, str | bool]:
    """新建文本文件；不自动覆盖已有文件，避免误伤用户数据。"""
    real_path = resolve_writable_path(user, path)
    if real_path.exists():
        raise validation_error("target already exists")
    ensure_parent_directory(real_path)
    real_path.write_text(content, encoding="utf-8")
    record_file_audit(user, "create", path)
    return {"accepted": True, "action": "create", "path": normalize_virtual_path(path)}


def save_text_file(user: UserRecord, path: str, content: str) -> dict[str, str | bool]:
    """保存已打开的文本文件，目标必须是普通文件或尚不存在的新文件。"""
    real_path = resolve_writable_path(user, path)
    if real_path.exists() and not real_path.is_file():
        raise validation_error("path is not a file")
    ensure_parent_directory(real_path)
    real_path.write_text(content, encoding="utf-8")
    record_file_audit(user, "save", path)
    return {"accepted": True, "action": "save", "path": normalize_virtual_path(path)}


def copy_path(user: UserRecord, path: str, target_path: str) -> dict[str, str | bool]:
    """复制文件或目录，目标存在时拒绝覆盖，降低批量操作风险。"""
    source = resolve_readable_existing_path(user, path)
    target = resolve_writable_path(user, target_path)
    if target.exists():
        raise validation_error("target already exists")
    ensure_parent_directory(target)
    if source.is_dir():
        ensure_not_child_target(source, target)
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    record_file_audit(user, "copy", path, target_path)
    return {"accepted": True, "action": "copy", "path": normalize_virtual_path(path), "target_path": normalize_virtual_path(target_path)}


def move_path(user: UserRecord, path: str, target_path: str) -> dict[str, str | bool]:
    """移动文件或目录；源路径不能是工作区根，目标不能已存在。"""
    source = resolve_readable_existing_path(user, path)
    target = resolve_writable_path(user, target_path)
    ensure_not_root_operation(user, path)
    if target.exists():
        raise validation_error("target already exists")
    if source.is_dir():
        ensure_not_child_target(source, target)
    ensure_parent_directory(target)
    shutil.move(str(source), str(target))
    record_file_audit(user, "move", path, target_path)
    return {"accepted": True, "action": "move", "path": normalize_virtual_path(path), "target_path": normalize_virtual_path(target_path)}


def delete_path(user: UserRecord, path: str) -> dict[str, str | bool]:
    """删除文件或目录；根目录和可见根目录受到保护，避免一次操作清空工作区。"""
    real_path = resolve_writable_existing_path(user, path)
    ensure_not_root_operation(user, path)
    if real_path.is_dir():
        shutil.rmtree(real_path)
    else:
        real_path.unlink()
    record_file_audit(user, "delete", path)
    return {"accepted": True, "action": "delete", "path": normalize_virtual_path(path)}


def get_latest_file_job(user: UserRecord) -> FileJobData | None:
    """返回当前用户最近一次打包/解压任务，状态来自数据库以支持刷新、重启和多 worker。"""
    require_permission(user.role, "files:read")
    with SessionLocal() as db:
        job = db.scalar(
            select(FileJobModel)
            .where(FileJobModel.user_id == user.id)
            .order_by(FileJobModel.created_at.desc(), FileJobModel.id.desc())
            .limit(1)
        )
        return file_job_to_data(job) if job else None


def mark_interrupted_file_jobs() -> int:
    """服务启动时把遗留的运行中任务标记为失败，避免重启后长期占用并发名额。"""
    now = utc_datetime()
    with SessionLocal() as db:
        jobs = db.scalars(select(FileJobModel).where(FileJobModel.state.in_(ACTIVE_JOB_STATES))).all()
        for job in jobs:
            job.state = "failed"
            job.message = "服务重启或工作进程退出，任务已中断"
            job.updated_at = now
            job.finished_at = now
        db.commit()
        return len(jobs)


def start_archive_job(user: UserRecord, path: str, target_path: str | None = None) -> FileJobData:
    """启动文件夹 zip 打包任务；同一用户同时只能运行一个打包或解压任务。"""
    require_permission(user.role, "files:write")
    source = resolve_readable_existing_path(user, path)
    normalized = normalize_virtual_path(path)
    if not source.is_dir():
        raise validation_error("path is not a directory")
    target_virtual = target_path or f"{normalized.rstrip('/')}.zip"
    target = resolve_writable_path(user, target_virtual)
    if target.exists():
        raise validation_error("target already exists")
    ensure_not_child_target(source, target)
    ensure_parent_directory(target)
    ensure_no_active_file_job(user)
    job = create_file_job(user, "archive", normalized, normalize_virtual_path(target_virtual))
    thread = threading.Thread(
        target=run_archive_job,
        args=(job.id, user, source, target, normalized, normalize_virtual_path(target_virtual)),
        daemon=True,
    )
    thread.start()
    return job


def start_extract_job(user: UserRecord, path: str, target_path: str | None = None) -> FileJobData:
    """启动压缩包解压任务；目标目录由前端目录选择器提供，仍由服务端校验边界。"""
    require_permission(user.role, "files:write")
    source = resolve_readable_existing_path(user, path)
    normalized = normalize_virtual_path(path)
    if not source.is_file() or archive_suffix(source) not in ARCHIVE_SUFFIXES:
        raise validation_error("unsupported archive type")
    target_virtual = target_path or parent_virtual_path(normalized)
    target = resolve_writable_path(user, target_virtual)
    if not target.exists() or not target.is_dir():
        raise validation_error("target path is not a directory")
    ensure_no_active_file_job(user)
    job = create_file_job(user, "extract", normalized, normalize_virtual_path(target_virtual))
    thread = threading.Thread(
        target=run_extract_job,
        args=(job.id, user, source, target, normalized, normalize_virtual_path(target_virtual)),
        daemon=True,
    )
    thread.start()
    return job


def archive_path(user: UserRecord, path: str, target_path: str | None = None) -> dict[str, str | bool]:
    """把文件或目录压缩为 zip，目标路径默认放在源路径同级目录。"""
    source = resolve_readable_existing_path(user, path)
    normalized = normalize_virtual_path(path)
    target_virtual = target_path or f"{normalized.rstrip('/')}.zip"
    target = resolve_writable_path(user, target_virtual)
    if target.exists():
        raise validation_error("target already exists")
    if source.is_dir():
        ensure_not_child_target(source, target)
    ensure_parent_directory(target)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if source.is_dir():
            for child in source.rglob("*"):
                archive.write(child, child.relative_to(source.parent).as_posix())
        else:
            archive.write(source, source.name)
    record_file_audit(user, "archive", path, target_virtual)
    return {"accepted": True, "action": "archive", "path": normalized, "target_path": normalize_virtual_path(target_virtual)}


def extract_archive(user: UserRecord, path: str, target_path: str | None = None) -> dict[str, str | bool]:
    """安全解压 zip/tar，拒绝绝对路径、上级目录和软链接成员。"""
    source = resolve_readable_existing_path(user, path)
    if not source.is_file():
        raise validation_error("path is not an archive file")
    target_virtual = target_path or parent_virtual_path(normalize_virtual_path(path))
    target = resolve_writable_path(user, target_virtual)
    target.mkdir(parents=True, exist_ok=True)

    suffix = archive_suffix(source)
    if suffix == ".zip":
        safe_extract_zip(source, target)
    elif suffix in ARCHIVE_SUFFIXES:
        safe_extract_tar(source, target)
    else:
        raise validation_error("unsupported archive type")
    record_file_audit(user, "extract", path, target_virtual)
    return {"accepted": True, "action": "extract", "path": normalize_virtual_path(path), "target_path": normalize_virtual_path(target_virtual)}


def build_download_path(user: UserRecord, path: str) -> Path:
    """为下载接口解析真实路径，目录下载需先打包，避免隐式流式遍历目录。"""
    real_path = resolve_readable_existing_path(user, path)
    if not real_path.is_file():
        raise validation_error("path is not a file")
    return real_path


def resolve_upload_target(user: UserRecord, directory_path: str, filename: str) -> Path:
    """根据上传目录和浏览器文件名计算最终落盘路径，丢弃客户端传来的目录片段。"""
    directory = resolve_writable_path(user, directory_path)
    if not directory.exists() or not directory.is_dir():
        raise validation_error("upload path is not a directory")
    safe_name = Path(filename or "").name
    if not safe_name or safe_name in {".", ".."}:
        raise validation_error("filename is invalid")
    target = directory / safe_name
    if target.exists():
        raise validation_error("target already exists")
    return target


def complete_upload(user: UserRecord, directory_path: str, filename: str) -> dict[str, str | bool]:
    """上传写入完成后记录审计；真实写入由 API 层按流式分块执行。"""
    normalized = normalize_virtual_path(directory_path)
    virtual_path = f"{normalized.rstrip('/')}/{Path(filename).name}"
    record_file_audit(user, "upload", normalized, virtual_path)
    return {"accepted": True, "action": "upload", "path": normalized, "target_path": virtual_path}


def resolve_readable_existing_path(user: UserRecord, path: str) -> Path:
    """解析可读路径并要求目标存在，便于读、复制、打包类操作复用。"""
    require_permission(user.role, "files:read")
    real_path = resolve_user_visible_path(path, user.username, user.role.value)
    if not real_path.exists():
        raise not_found("path not found")
    return real_path


def resolve_writable_path(user: UserRecord, path: str) -> Path:
    """解析可写路径；权限检查集中在这里，避免各操作遗漏 RBAC。"""
    require_permission(user.role, "files:write")
    return resolve_user_visible_path(path, user.username, user.role.value)


def resolve_writable_existing_path(user: UserRecord, path: str) -> Path:
    """解析可写路径并要求目标存在，供删除和移动前置校验使用。"""
    real_path = resolve_writable_path(user, path)
    if not real_path.exists():
        raise not_found("path not found")
    return real_path


def ensure_parent_directory(path: Path) -> None:
    """写文件前必须确认父目录存在，避免拼错路径时自动创建意外层级。"""
    if not path.parent.exists() or not path.parent.is_dir():
        raise validation_error("parent directory does not exist")


def ensure_not_root_operation(user: UserRecord, path: str) -> None:
    """保护虚拟根、用户 home 和共享可见根，防止删除或移动根目录。"""
    normalized = normalize_virtual_path(path)
    real_path = resolve_user_visible_path(normalized, user.username, user.role.value)
    settings = get_settings()
    protected_roots = {Path(settings.user_home_root).resolve(strict=False)}
    protected_roots.update(Path(root).resolve(strict=False) for root in settings.visible_roots)
    if user.role.value == "admin":
        protected_roots.add(Path(f"/home/{settings.main_linux_user}").resolve(strict=False))
    else:
        protected_roots.add((Path(settings.user_home_root) / user.username).resolve(strict=False))
    if normalized == "/" or real_path in protected_roots:
        raise validation_error("refusing to operate on protected root")


def safe_extract_zip(source: Path, target: Path) -> None:
    """解压 zip 前逐项校验，避免压缩包利用 ../ 写出目标目录。"""
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            ensure_safe_archive_name(member.filename)
            destination = (target / member.filename).resolve(strict=False)
            ensure_child_path(destination, target)
        archive.extractall(target)


def safe_extract_tar(source: Path, target: Path) -> None:
    """解压 tar 前拒绝链接和路径逃逸，tar 链接成员容易造成越界写入。"""
    with tarfile.open(source, "r:*") as archive:
        for member in archive.getmembers():
            ensure_safe_tar_member(member, target)
        archive.extractall(target)


def ensure_safe_archive_name(name: str) -> None:
    """压缩包成员名必须是相对路径，不能包含上级目录片段。"""
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        raise validation_error("archive contains unsafe path")


def ensure_child_path(path: Path, root: Path) -> None:
    """确认目标路径仍在解压目录内，作为成员名检查之外的第二道保险。"""
    resolved_root = root.resolve(strict=False)
    if path != resolved_root and resolved_root not in path.parents:
        raise validation_error("archive escapes target directory")


def ensure_not_child_target(source: Path, target: Path) -> None:
    """拒绝把目录复制或移动到自身内部，避免递归复制和不可恢复的目录结构。"""
    resolved_source = source.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if resolved_target == resolved_source or resolved_source in resolved_target.parents:
        raise validation_error("target cannot be inside source directory")


def ensure_no_active_file_job(user: UserRecord) -> None:
    """限制单用户和全局重 IO 文件任务并发，避免多进程部署时打满共享盘。"""
    with SessionLocal() as db:
        user_active = db.scalar(
            select(func.count())
            .select_from(FileJobModel)
            .where(FileJobModel.user_id == user.id)
            .where(FileJobModel.state.in_(ACTIVE_JOB_STATES))
        ) or 0
        if user_active:
            raise validation_error("file job already running")
        global_active = db.scalar(
            select(func.count())
            .select_from(FileJobModel)
            .where(FileJobModel.state.in_(ACTIVE_JOB_STATES))
        ) or 0
        if global_active >= MAX_ACTIVE_FILE_JOBS:
            raise validation_error("too many file jobs running")


def create_file_job(user: UserRecord, action: str, source_path: str, target_path: str) -> FileJobData:
    """登记新的文件任务到数据库，保留历史记录并给前端展示最近一次任务。"""
    now = utc_datetime()
    with SessionLocal() as db:
        existing = db.scalar(
            select(func.count())
            .select_from(FileJobModel)
            .where(FileJobModel.user_id == user.id)
            .where(FileJobModel.state.in_(ACTIVE_JOB_STATES))
        ) or 0
        if existing:
            raise validation_error("file job already running")
        job = FileJobModel(
            id=uuid4().hex,
            user_id=user.id,
            action=action,
            source_path=source_path,
            target_path=target_path,
            state="pending",
            progress=0,
            current_file="",
            message="等待开始",
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        data = file_job_to_data(job)
    return data


def run_archive_job(
    job_id: str,
    user: UserRecord,
    source: Path,
    target: Path,
    source_virtual: str,
    target_virtual: str,
) -> None:
    """在后台执行 zip 打包，优先调用系统 zip 命令，缺失时回退到 Python 实现。"""
    try:
        update_file_job(user.id, job_id, state="running", progress=1, message="开始打包")
        total = max(1, count_directory_entries(source))
        if shutil.which("zip"):
            run_zip_command_job(user.id, job_id, source, target, total)
        else:
            run_python_zip_job(user.id, job_id, source, target, total)
        update_file_job(user.id, job_id, state="succeeded", progress=100, current_file=target_virtual, message="打包完成", finished=True)
        record_file_audit(user, "archive", source_virtual, target_virtual)
    except Exception as exc:  # noqa: BLE001 - 后台线程需要把所有异常收敛为可展示状态。
        cleanup_partial_file(target)
        update_file_job(user.id, job_id, state="failed", message=str(exc), finished=True)


def run_zip_command_job(user_id: int, job_id: str, source: Path, target: Path, total: int) -> None:
    """调用系统 zip -r 生成压缩包，并根据输出条目数近似更新进度。"""
    process = subprocess.Popen(
        ["zip", "-r", str(target), source.name],
        cwd=str(source.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    processed = 0
    assert process.stdout is not None
    for line in process.stdout:
        text = line.strip()
        if not text:
            continue
        processed += 1
        progress = min(99, max(1, int(processed / total * 100)))
        update_file_job(user_id, job_id, progress=progress, current_file=text, message="正在打包")
    return_code = process.wait()
    if return_code != 0:
        raise validation_error("zip command failed", data={"returncode": return_code})


def run_python_zip_job(user_id: int, job_id: str, source: Path, target: Path, total: int) -> None:
    """开发环境没有 zip 命令时的兜底实现，逐条写入以便持续更新进度。"""
    entries = [source, *source.rglob("*")]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, child in enumerate(entries, start=1):
            arcname = child.relative_to(source.parent).as_posix()
            archive.write(child, arcname)
            progress = min(99, max(1, int(index / total * 100)))
            update_file_job(user_id, job_id, progress=progress, current_file=arcname, message="正在打包")


def run_extract_job(
    job_id: str,
    user: UserRecord,
    source: Path,
    target: Path,
    source_virtual: str,
    target_virtual: str,
) -> None:
    """在后台逐项安全解压 zip/tar 文件，拒绝路径逃逸和软链接成员。"""
    try:
        update_file_job(user.id, job_id, state="running", progress=1, message="开始解压")
        suffix = archive_suffix(source)
        if suffix == ".zip":
            run_zip_extract_job(user.id, job_id, source, target)
        elif suffix in ARCHIVE_SUFFIXES:
            run_tar_extract_job(user.id, job_id, source, target)
        else:
            raise validation_error("unsupported archive type")
        update_file_job(user.id, job_id, state="succeeded", progress=100, current_file=target_virtual, message="解压完成", finished=True)
        record_file_audit(user, "extract", source_virtual, target_virtual)
    except Exception as exc:  # noqa: BLE001 - 后台线程需要把所有异常收敛为可展示状态。
        update_file_job(user.id, job_id, state="failed", message=str(exc), finished=True)


def run_zip_extract_job(user_id: int, job_id: str, source: Path, target: Path) -> None:
    """逐项安全解压 zip 文件，便于把每个成员写入数据库进度。"""
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        total = max(1, len(members))
        for member in members:
            ensure_safe_zip_member(member, target)
        for index, member in enumerate(members, start=1):
            archive.extract(member, target)
            progress = min(99, max(1, int(index / total * 100)))
            update_file_job(user_id, job_id, progress=progress, current_file=member.filename, message="正在解压")


def run_tar_extract_job(user_id: int, job_id: str, source: Path, target: Path) -> None:
    """逐项安全解压 tar 系列文件，拒绝软链接、硬链接和路径逃逸。"""
    with tarfile.open(source, "r:*") as archive:
        members = archive.getmembers()
        total = max(1, len(members))
        for member in members:
            ensure_safe_tar_member(member, target)
        for index, member in enumerate(members, start=1):
            archive.extract(member, target)
            progress = min(99, max(1, int(index / total * 100)))
            update_file_job(user_id, job_id, progress=progress, current_file=member.name, message="正在解压")


def update_file_job(
    user_id: int,
    job_id: str,
    *,
    state: str | None = None,
    progress: int | None = None,
    current_file: str | None = None,
    message: str | None = None,
    finished: bool = False,
) -> None:
    """把任务进度写入数据库，供刷新、重启和多 worker 读取。"""
    now = utc_datetime()
    with SessionLocal() as db:
        job = db.get(FileJobModel, job_id)
        if job is None or job.user_id != user_id:
            return
        if state is not None:
            job.state = state
        if progress is not None:
            job.progress = max(0, min(100, progress))
        if current_file is not None:
            job.current_file = current_file
        if message is not None:
            job.message = message
        job.updated_at = now
        if finished:
            job.finished_at = now
        db.commit()


def count_directory_entries(path: Path) -> int:
    """统计目录打包条目数，用于给 zip 命令输出估算百分比。"""
    return 1 + sum(1 for _ in path.rglob("*"))


def ensure_safe_zip_member(member: zipfile.ZipInfo, target: Path) -> None:
    """校验 zip 成员不会逃逸目标目录，也不允许软链接写入。"""
    ensure_safe_archive_name(member.filename)
    mode = member.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise validation_error("archive links are not allowed")
    destination = (target / member.filename).resolve(strict=False)
    ensure_child_path(destination, target)


def ensure_safe_tar_member(member: tarfile.TarInfo, target: Path) -> None:
    """校验 tar 成员不会通过路径、软链接或硬链接写出目标目录。"""
    ensure_safe_archive_name(member.name)
    if member.issym() or member.islnk():
        raise validation_error("archive links are not allowed")
    if not (member.isfile() or member.isdir()):
        raise validation_error("archive member type is not allowed")
    destination = (target / member.name).resolve(strict=False)
    ensure_child_path(destination, target)


def archive_suffix(path: Path) -> str:
    """返回规范化压缩包后缀，统一 zip/tar/tar.gz/tgz 等判断。"""
    suffix = path.suffix.lower()
    suffixes = [item.lower() for item in path.suffixes]
    combined = "".join(suffixes[-2:])
    if combined in {".tar.gz", ".tar.bz2", ".tar.xz"}:
        return combined
    return suffix


def cleanup_partial_file(path: Path) -> None:
    """打包失败时删除半成品 zip，避免用户误下载损坏文件。"""
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        pass


def file_job_to_data(job: FileJobModel) -> FileJobData:
    """把数据库任务状态转换为 API 响应模型。"""
    return FileJobData(
        id=job.id,
        action=job.action,
        source_path=job.source_path,
        target_path=job.target_path,
        state=job.state,
        progress=job.progress,
        current_file=job.current_file,
        message=job.message,
        created_at=datetime_to_iso(job.created_at),
        updated_at=datetime_to_iso(job.updated_at or job.created_at),
        finished_at=datetime_to_iso(job.finished_at) if job.finished_at else None,
    )


def utc_datetime() -> datetime:
    """返回系统本地时区时间，统一文件任务数据库时间字段。"""
    return local_datetime()


def datetime_to_iso(value: datetime) -> str:
    """把数据库时间转换为前端稳定消费的 ISO 字符串。"""
    return ensure_local_datetime(value).isoformat()


def record_file_audit(user: UserRecord, action: str, path: str, target_path: str | None = None) -> None:
    """文件写操作统一审计，便于后续追踪危险操作来源。"""
    record_audit(
        user.id,
        f"file.{action}",
        "file",
        normalize_virtual_path(path),
        detail_json={"target_path": normalize_virtual_path(target_path) if target_path else None},
    )


def build_file_entry(path: Path, parent_virtual_path: str) -> FileEntry:
    """把真实文件系统条目转换为不暴露真实根路径的虚拟文件项。"""
    stat = path.stat()
    virtual_parent = parent_virtual_path.rstrip("/")
    virtual_path = f"{virtual_parent}/{path.name}" if virtual_parent else f"/{path.name}"
    modified = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
    return FileEntry(
        name=path.name,
        path=virtual_path,
        type="directory" if path.is_dir() else "file",
        size_bytes=0 if path.is_dir() else stat.st_size,
        modified_at=modified,
    )


def file_sort_key(path: Path) -> tuple[int, str]:
    """目录排在文件前，并按名称排序，匹配常见文件管理器体验。"""
    return (0 if path.is_dir() else 1, path.name.lower())


def guess_content_type(path: Path) -> str:
    """使用标准 mimetype 推断预览类型，未知类型按二进制处理。"""
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def is_text_file(path: Path, content_type: str) -> bool:
    """文本类型和常见脚本配置后缀允许进入编辑器，其余文件只预览。"""
    return content_type.startswith("text/") or path.suffix.lower() in EDITABLE_TEXT_TYPES


def parent_virtual_path(path: str) -> str:
    """计算虚拟父路径，根目录的父级保持为根目录。"""
    parent = str(PurePosixPath(path).parent)
    return parent if parent != "." else "/"
