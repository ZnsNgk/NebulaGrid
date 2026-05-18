from enum import StrEnum

from app.core.errors import forbidden


class Role(StrEnum):
    """平台角色枚举，后续会与数据库 users.role 字段保持一致。"""

    STUDENT = "student"
    MENTOR = "mentor"
    ADMIN = "admin"
    VIEWER = "viewer"


ROLE_PERMISSIONS: dict[Role, set[str]] = {
    Role.STUDENT: {
        "dashboard:read",
        "nodes:read",
        "tasks:read",
        "tasks:create",
        "files:read",
        "files:write",
        "envs:read",
        "envs:write",
    },
    Role.MENTOR: {
        "dashboard:read",
        "nodes:read",
        "tasks:read",
        "tasks:create",
        "files:read",
        "files:write",
        "envs:read",
        "envs:write",
        "users:read",
        "users:create_student",
    },
    Role.ADMIN: {"*"},
    Role.VIEWER: {"dashboard:read", "nodes:read", "tasks:read", "envs:read"},
}


def list_permissions(role: Role) -> list[str]:
    """返回角色权限列表，管理员用通配符表达拥有全部权限。"""
    return sorted(ROLE_PERMISSIONS.get(role, set()))


def has_permission(role: Role, permission: str) -> bool:
    """判断角色是否拥有指定权限，服务层可据此做强制 RBAC 校验。"""
    permissions = ROLE_PERMISSIONS.get(role, set())
    return "*" in permissions or permission in permissions


def require_permission(role: Role, permission: str) -> None:
    """在服务层断言权限，失败时抛出统一 403 业务异常。"""
    if not has_permission(role, permission):
        raise forbidden(f"permission required: {permission}")
