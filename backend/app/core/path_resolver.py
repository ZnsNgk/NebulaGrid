from pathlib import Path

from app.core.config import get_settings
from app.core.errors import forbidden, validation_error


def normalize_virtual_path(path: str) -> str:
    """规范化用户提交的虚拟路径，防止空路径和相对路径绕过边界检查。"""
    if not path or not path.startswith("/"):
        raise validation_error("path must be an absolute virtual path")
    return str(Path(path).as_posix())


def resolve_visible_path(path: str) -> Path:
    """把虚拟路径解析为真实路径，并限制在配置允许的可见根目录内。"""
    settings = get_settings()
    normalized = normalize_virtual_path(path)
    real_path = Path(normalized).resolve(strict=False)
    allowed_roots = [Path(root).resolve(strict=False) for root in settings.visible_roots]
    if not any(real_path == root or root in real_path.parents for root in allowed_roots):
        raise forbidden("path is outside visible roots")
    return real_path


def resolve_user_visible_path(path: str, user_id: int) -> Path:
    """解析用户可见路径，把 /workspace 安全映射到配置的用户 home 根目录。"""
    settings = get_settings()
    normalized = normalize_virtual_path(path)
    alias = settings.user_workspace_alias.rstrip("/")
    if normalized == alias or normalized.startswith(f"{alias}/"):
        suffix = normalized[len(alias) :].lstrip("/")
        candidate = Path(settings.user_home_root) / str(user_id) / suffix
    else:
        candidate = Path(normalized)
    real_path = candidate.resolve(strict=False)
    user_root = (Path(settings.user_home_root) / str(user_id)).resolve(strict=False)
    shared_roots = [Path(root).resolve(strict=False) for root in settings.visible_roots]
    allowed_roots = [user_root, *shared_roots]
    if not any(real_path == root or root in real_path.parents for root in allowed_roots):
        raise forbidden("path is outside allowed user roots")
    return real_path
