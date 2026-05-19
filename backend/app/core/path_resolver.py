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


def resolve_user_visible_path(path: str, username: str, role: str = "student") -> Path:
    """解析用户可见路径，把虚拟根目录 / 安全映射到当前用户文件根目录。"""
    settings = get_settings()
    normalized = normalize_virtual_path(path)
    if role == "admin":
        user_root = Path(f"/home/{settings.main_linux_user}").resolve(strict=False)
    else:
        user_root = (Path(settings.user_home_root) / username).resolve(strict=False)
    user_home_root = Path(settings.user_home_root).resolve(strict=False)
    shared_roots = [
        root
        for root in (Path(item).resolve(strict=False) for item in settings.visible_roots)
        if root != user_home_root
    ]
    allowed_roots = [user_root, *shared_roots]

    # 文件管理不再暴露 /workspace 概念。普通的 /project/train.py 这类虚拟路径
    # 都落到用户文件根目录下；只有已经位于共享可见根中的真实绝对路径才按原路径解析。
    absolute_candidate = Path(normalized).resolve(strict=False)
    if normalized == "/":
        candidate = user_root
    elif any(absolute_candidate == root or root in absolute_candidate.parents for root in allowed_roots):
        candidate = absolute_candidate
    else:
        candidate = user_root / normalized.lstrip("/")

    real_path = candidate.resolve(strict=False)
    if not any(real_path == root or root in real_path.parents for root in allowed_roots):
        raise forbidden("path is outside allowed user roots")
    return real_path
