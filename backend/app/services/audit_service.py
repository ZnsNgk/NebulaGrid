from datetime import datetime, timezone
from itertools import count
from typing import Any

from app.core.rbac import require_permission
from app.schemas.admin import AuditLogInfo, SettingInfo
from app.services.auth_service import UserRecord

_AUDIT_ID = count(1)
_AUDIT_LOGS: list[AuditLogInfo] = []
_SETTINGS: dict[str, SettingInfo] = {
    "scheduler.enabled": SettingInfo(key="scheduler.enabled", value="true"),
    "uploads.max_size_mb": SettingInfo(key="uploads.max_size_mb", value="1024"),
}


def utc_now() -> str:
    """返回 UTC ISO 时间字符串，统一内存数据的时间表示。"""
    return datetime.now(timezone.utc).isoformat()


def record_audit(
    actor_user_id: int,
    action: str,
    target_type: str,
    target_id: str,
    result: str = "success",
    detail_json: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLogInfo:
    """记录审计日志，保证危险动作和关键变更可追溯。"""
    entry = AuditLogInfo(
        id=next(_AUDIT_ID),
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        ip=ip,
        result=result,
        created_at=utc_now(),
        detail_json=detail_json or {},
    )
    _AUDIT_LOGS.append(entry)
    return entry


def list_audit_logs(user: UserRecord, page: int, page_size: int) -> tuple[list[AuditLogInfo], int]:
    """分页返回审计日志，只有管理员可以查看全局审计信息。"""
    require_permission(user.role, "admin:audit:read")
    start = (page - 1) * page_size
    end = start + page_size
    return list(reversed(_AUDIT_LOGS))[start:end], len(_AUDIT_LOGS)


def list_settings(user: UserRecord) -> list[SettingInfo]:
    """返回系统配置项，管理员用于运维面板展示当前配置。"""
    require_permission(user.role, "admin:settings:read")
    return sorted(_SETTINGS.values(), key=lambda item: item.key)


def update_settings(user: UserRecord, values: dict[str, str]) -> list[SettingInfo]:
    """更新受控系统配置，并为每个键记录最后修改人和修改时间。"""
    require_permission(user.role, "admin:settings:write")
    now = utc_now()
    for key, value in values.items():
        _SETTINGS[key] = SettingInfo(key=key, value=value, updated_by=user.id, updated_at=now)
    record_audit(user.id, "settings.update", "settings", ",".join(values.keys()))
    return list_settings(user)

