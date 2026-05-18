from datetime import datetime, timezone
from pathlib import Path

from app.core.errors import validation_error
from app.core.path_resolver import resolve_user_visible_path
from app.core.rbac import require_permission
from app.schemas.files import FileEntry, FileListData, FilePreviewData
from app.services.audit_service import record_audit
from app.services.auth_service import UserRecord


def list_files(user: UserRecord, path: str) -> FileListData:
    """列出用户可见目录，路径解析统一经过 PathResolver。"""
    require_permission(user.role, "files:read")
    real_path = resolve_user_visible_path(path, user.id)
    if not real_path.exists():
        return FileListData(path=path, items=[])
    if not real_path.is_dir():
        raise validation_error("path is not a directory")
    items = [build_file_entry(child, path) for child in sorted(real_path.iterdir())]
    return FileListData(path=path, items=items)


def preview_file(user: UserRecord, path: str, limit: int = 65536) -> FilePreviewData:
    """预览文本文件前若干字节，避免一次性读取过大文件。"""
    require_permission(user.role, "files:read")
    real_path = resolve_user_visible_path(path, user.id)
    if not real_path.exists() or not real_path.is_file():
        raise validation_error("path is not a file")
    content = real_path.read_text(encoding="utf-8", errors="replace")[:limit]
    truncated = real_path.stat().st_size > limit
    return FilePreviewData(path=path, content_type="text/plain", content=content, truncated=truncated)


def acknowledge_write_operation(
    user: UserRecord,
    action: str,
    path: str,
    target_path: str | None = None,
) -> dict[str, str | bool | None]:
    """校验写操作路径并记录审计，真实文件变更后续再逐项实现。"""
    require_permission(user.role, "files:write")
    resolve_user_visible_path(path, user.id)
    if target_path:
        resolve_user_visible_path(target_path, user.id)
    record_audit(user.id, f"file.{action}", "file", path, detail_json={"target_path": target_path})
    return {"accepted": True, "action": action, "path": path, "target_path": target_path}


def build_file_entry(path: Path, parent_virtual_path: str) -> FileEntry:
    """把真实文件系统条目转换为不暴露真实根路径的虚拟文件项。"""
    stat = path.stat()
    virtual_parent = parent_virtual_path.rstrip("/")
    virtual_path = f"{virtual_parent}/{path.name}" if virtual_parent else f"/{path.name}"
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    return FileEntry(
        name=path.name,
        path=virtual_path,
        type="directory" if path.is_dir() else "file",
        size_bytes=0 if path.is_dir() else stat.st_size,
        modified_at=modified,
    )

