from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, cast, func, or_, select

from app.core.rbac import require_permission
from app.db.models import AuditLog, Setting, User
from app.db.session import SessionLocal
from app.schemas.admin import AuditLogInfo, SettingInfo
from app.services.auth_service import UserRecord

DEFAULT_SETTINGS: dict[str, str] = {
    "scheduler.enabled": "true",
    "monitor.enabled": "true",
    "uploads.max_size_mb": "20480",
}

AUDIT_CATEGORIES = {"all", "system", "user", "archive", "file", "task", "env", "node", "other"}


def utc_now() -> str:
    """返回 UTC ISO 时间字符串，统一审计和设置更新时间格式。"""
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
    """把关键操作写入数据库审计表，避免进程重启后审计记录丢失。"""
    with SessionLocal() as db:
        entry = AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ip=ip,
            result=result,
            detail_json=detail_json or {},
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return audit_log_to_info(entry)


def list_audit_logs(
    user: UserRecord,
    page: int,
    page_size: int,
    category: str | None = None,
    keyword: str | None = None,
    action: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> tuple[list[AuditLogInfo], int]:
    """分页返回全局审计日志，并按分类、用户、动作和时间范围缩小排查范围。"""
    require_permission(user.role, "admin:audit:read")
    safe_category = category if category in AUDIT_CATEGORIES else "all"
    conditions = [item for item in [audit_category_conditions(safe_category)] if item is not None]
    cleaned_keyword = (keyword or "").strip()
    cleaned_action = (action or "").strip()
    start_at = parse_audit_time(start_time)
    end_at = parse_audit_time(end_time)
    if cleaned_keyword:
        keyword_like = f"%{cleaned_keyword}%"
        keyword_conditions = [
            AuditLog.action.ilike(keyword_like),
            AuditLog.target_type.ilike(keyword_like),
            AuditLog.target_id.ilike(keyword_like),
            AuditLog.ip.ilike(keyword_like),
            AuditLog.result.ilike(keyword_like),
            cast(AuditLog.detail_json, String).ilike(keyword_like),
            User.username.ilike(keyword_like),
            User.real_name.ilike(keyword_like),
        ]
        if cleaned_keyword.isdigit():
            keyword_conditions.append(AuditLog.actor_user_id == int(cleaned_keyword))
        conditions.append(or_(*keyword_conditions))
    if cleaned_action:
        conditions.append(AuditLog.action.ilike(f"%{cleaned_action}%"))
    if start_at is not None:
        conditions.append(AuditLog.created_at >= start_at)
    if end_at is not None:
        conditions.append(AuditLog.created_at <= end_at)
    with SessionLocal() as db:
        # 关联用户表只用于筛选操作者账号/姓名，审计响应仍保持原有轻量字段结构。
        total_stmt = select(func.count()).select_from(AuditLog).outerjoin(User, AuditLog.actor_user_id == User.id)
        list_stmt = select(AuditLog).outerjoin(User, AuditLog.actor_user_id == User.id).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        if conditions:
            total_stmt = total_stmt.where(*conditions)
            list_stmt = list_stmt.where(*conditions)
        total = int(db.scalar(total_stmt) or 0)
        rows = db.scalars(list_stmt.offset((page - 1) * page_size).limit(page_size)).all()
        return [audit_log_to_info(row) for row in rows], total


def parse_audit_time(value: str | None) -> datetime | None:
    """解析前端时间范围；无效时间直接忽略，避免一次错误输入阻断审计页面。"""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def audit_category_conditions(category: str):
    """按管理员后台分类构造数据库查询条件，减少前端一次性拉取的日志量。"""
    if category == "all":
        return None
    if category == "system":
        return or_(AuditLog.target_type.in_(["system", "settings"]), AuditLog.action.startswith("settings."))
    if category == "user":
        return or_(
            AuditLog.target_type.in_(["user", "login_session"]),
            AuditLog.action.startswith("user."),
            AuditLog.action.startswith("auth."),
        )
    if category == "archive":
        return AuditLog.action.in_(["file.archive", "file.extract"])
    if category == "file":
        return AuditLog.target_type == "file"
    if category == "task":
        return AuditLog.target_type == "task"
    if category == "env":
        return or_(AuditLog.target_type.in_(["env", "env_package", "env_install_job"]), AuditLog.action.startswith("env."))
    if category == "node":
        return AuditLog.target_type == "node"
    return ~or_(
        AuditLog.target_type.in_(
            ["system", "settings", "user", "login_session", "file", "task", "env", "env_package", "env_install_job", "node"]
        ),
        AuditLog.action.startswith("settings."),
        AuditLog.action.startswith("user."),
        AuditLog.action.startswith("auth."),
        AuditLog.action.startswith("env."),
    )


def audit_category(action: str, target_type: str) -> str:
    """把审计动作归入管理员后台展示分类，新增动作默认进入其他类别。"""
    if target_type in {"system", "settings"} or action.startswith("settings."):
        return "system"
    if target_type in {"user", "login_session"} or action.startswith(("user.", "auth.")):
        return "user"
    if action in {"file.archive", "file.extract"}:
        return "archive"
    if target_type == "file":
        return "file"
    if target_type == "task":
        return "task"
    if target_type in {"env", "env_package", "env_install_job"} or action.startswith("env."):
        return "env"
    if target_type == "node":
        return "node"
    return "other"


def audit_log_to_info(entry: AuditLog) -> AuditLogInfo:
    """将数据库审计行转换成 API 响应，统一时间格式和分类字段。"""
    created_at = entry.created_at or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return AuditLogInfo(
        id=entry.id,
        actor_user_id=entry.actor_user_id,
        action=entry.action,
        target_type=entry.target_type,
        target_id=entry.target_id,
        ip=entry.ip,
        result=entry.result,
        created_at=created_at.astimezone(timezone.utc).isoformat(),
        detail_json=entry.detail_json or {},
        category=audit_category(entry.action, entry.target_type),
    )


def list_settings(user: UserRecord) -> list[SettingInfo]:
    """返回系统配置项，管理员用于运维面板展示当前配置。"""
    require_permission(user.role, "admin:settings:read")
    with SessionLocal() as db:
        ensure_default_settings(db)
        rows = db.scalars(select(Setting).order_by(Setting.key.asc())).all()
        return [setting_to_info(row) for row in rows]


def update_settings(user: UserRecord, values: dict[str, str]) -> list[SettingInfo]:
    """把系统配置更新到数据库，并记录最后修改人和审计日志。"""
    require_permission(user.role, "admin:settings:write")
    cleaned_values = {key.strip(): value for key, value in values.items() if key and key.strip()}
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        ensure_default_settings(db)
        for key, value in cleaned_values.items():
            row = db.get(Setting, key)
            if row is None:
                row = Setting(key=key, value=str(value), updated_by=user.id, updated_at=now)
                db.add(row)
            else:
                row.value = str(value)
                row.updated_by = user.id
                row.updated_at = now
        db.commit()
    if cleaned_values:
        record_audit(user.id, "settings.update", "settings", ",".join(sorted(cleaned_values)))
    return list_settings(user)


def ensure_default_settings(db) -> None:
    """补齐系统默认配置；只新增缺失键，避免覆盖管理员已经保存到数据库的值。"""
    changed = False
    for key, value in DEFAULT_SETTINGS.items():
        if db.get(Setting, key) is not None:
            continue
        db.add(Setting(key=key, value=value))
        changed = True
    if changed:
        db.commit()


def setting_to_info(row: Setting) -> SettingInfo:
    """把设置表记录转换为 API 响应，隐藏数据库对象并统一时间格式。"""
    updated_at = row.updated_at
    if updated_at is not None and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return SettingInfo(
        key=row.key,
        value=row.value,
        updated_by=row.updated_by,
        updated_at=updated_at.astimezone(timezone.utc).isoformat() if updated_at else None,
    )
