from dataclasses import fields
from datetime import datetime
from typing import Any

from sqlalchemy import String, cast, func, or_, select

from app.core.config import get_settings
from app.core.rbac import require_permission
from app.core.time_utils import ensure_local_datetime, local_datetime, local_now, parse_datetime_local
from app.db.models import AuditLog, Setting, User
from app.db.session import SessionLocal
from app.schemas.admin import AuditLogInfo, SettingInfo
from app.services.auth_service import UserRecord

SETTING_DESCRIPTIONS: dict[str, str] = {
    "scheduler.enabled": "控制任务调度器是否从等待队列领取新任务；关闭后不影响已运行任务。",
    "scheduler.instance_lock": "调度器实例锁占位行，用于防止多个调度器同时分配同一批任务。",
    "scheduler.interval_seconds": "调度器扫描等待任务的时间间隔，单位为秒；支持 0.5 这类小数值。",
    "monitor.enabled": "控制节点监控轮询是否启用；关闭后节点状态和指标不会自动刷新。",
    "uploads.max_size_mb": "Web 上传文件的建议最大体积，单位为 MB。",
    "app.name": "前端和健康检查中展示的系统名称。",
    "app.version": "当前 NebulaGrid 服务版本号，用于运维识别部署版本。",
    "environment": "当前运行环境标识，通常用于区分开发、测试和生产部署。",
    "data.root": "NebulaGrid 数据根目录，用户目录、日志和运行时数据通常放在此目录下。",
    "user.home_root": "教学用户主目录根路径，新建学生和导师账号时会基于它生成 home。",
    "visible.roots": "文件管理器允许展示的根目录列表，多个目录用英文逗号分隔。",
    "database.url": "后端主数据库连接地址；通常由环境变量或部署密钥管理。",
    "redis.url": "Redis 连接地址，用于后续缓存、队列或会话扩展。",
    "influxdb.url": "InfluxDB 服务地址，用于写入和读取节点 GPU 指标。",
    "influxdb.org": "InfluxDB 组织名称。",
    "influxdb.bucket": "InfluxDB 指标桶名称。",
    "influxdb.token": "InfluxDB 访问令牌；属于敏感值，生产环境应优先通过密钥注入。",
    "influxdb.latest_range": "读取最新指标时向前查询的时间窗口，例如 30m。",
    "task.log_root": "任务运行日志保存目录。",
    "conda.env_root": "Miniconda 环境目录，注册和扫描环境时会基于该路径。",
    "env.package_root": "用户上传环境包的保存目录。",
    "env.install_log_root": "环境包安装日志保存目录。",
    "runtime.root": "任务运行时元数据、状态文件和临时文件的根目录。",
    "remote.code_root": "同步到计算节点的远端脚本目录。",
    "miniconda.python": "主节点 Miniconda Python 解释器路径。",
    "main.linux_user": "主控 Linux 账号名，管理员账号通常映射到该系统用户。",
    "manage.linux_accounts": "是否由 NebulaGrid 自动创建和维护 Linux 子账号。",
    "manage.samba_accounts": "是否由 NebulaGrid 自动执行 smbpasswd/pdbedit 来维护用户 Samba 账号；关闭时只记录用户期望状态，不改动系统 Samba 数据库。",
    "session.secret": "登录会话签名密钥；生产环境应使用外部密钥并定期轮换。",
    "monitor.interval_seconds": "节点监控远端循环输出间隔，单位为秒；worker 会为每个可监控节点保持 SSH 长连接。",
    "monitor.reconnect_attempts": "节点监控 SSH 长连接断开后的自动重连次数；达到上限后节点保持离线并停止继续连接。",
    "monitor.watchdog_timeout_seconds": "节点监控长连接未收到有效状态 JSON 的超时时间，单位为秒；超时后节点先置为离线并触发自动重连。",
    "cors.origins": "允许访问 API 的前端来源列表，多个来源用英文逗号分隔。",
}

SETTING_VALUE_TYPES: dict[str, str] = {
    "scheduler.enabled": "boolean",
    "monitor.enabled": "boolean",
    "manage.linux_accounts": "boolean",
    "manage.samba_accounts": "boolean",
    "scheduler.interval_seconds": "number",
    "monitor.interval_seconds": "integer",
    "monitor.reconnect_attempts": "integer",
    "monitor.watchdog_timeout_seconds": "integer",
    "uploads.max_size_mb": "integer",
}

SETTING_OPTIONS: dict[str, list[dict[str, str]]] = {
    "scheduler.enabled": [{"value": "true", "label": "开启"}, {"value": "false", "label": "关闭"}],
    "monitor.enabled": [{"value": "true", "label": "开启"}, {"value": "false", "label": "关闭"}],
    "manage.linux_accounts": [{"value": "true", "label": "开启"}, {"value": "false", "label": "关闭"}],
    "manage.samba_accounts": [{"value": "true", "label": "开启"}, {"value": "false", "label": "关闭"}],
    "environment": [
        {"value": "development", "label": "开发环境"},
        {"value": "testing", "label": "测试环境"},
        {"value": "production", "label": "生产环境"},
    ],
}

SETTING_KEY_ALIASES: dict[str, str] = {
    "app_name": "app.name",
    "app_version": "app.version",
    "data_root": "data.root",
    "user_home_root": "user.home_root",
    "visible_roots": "visible.roots",
    "database_url": "database.url",
    "redis_url": "redis.url",
    "influxdb_url": "influxdb.url",
    "influxdb_org": "influxdb.org",
    "influxdb_bucket": "influxdb.bucket",
    "influxdb_token": "influxdb.token",
    "influxdb_latest_range": "influxdb.latest_range",
    "task_log_root": "task.log_root",
    "conda_env_root": "conda.env_root",
    "env_package_root": "env.package_root",
    "env_install_log_root": "env.install_log_root",
    "runtime_root": "runtime.root",
    "remote_code_root": "remote.code_root",
    "miniconda_python": "miniconda.python",
    "main_linux_user": "main.linux_user",
    "manage_linux_accounts": "manage.linux_accounts",
    "manage_samba_accounts": "manage.samba_accounts",
    "session_secret": "session.secret",
    "scheduler_interval_seconds": "scheduler.interval_seconds",
    "monitor_interval_seconds": "monitor.interval_seconds",
    "monitor_reconnect_attempts": "monitor.reconnect_attempts",
    "monitor_watchdog_timeout_seconds": "monitor.watchdog_timeout_seconds",
    "cors_origins": "cors.origins",
}

# 这些配置承载数据库、缓存、指标库连接信息或会话密钥，只允许通过环境变量/
# 部署密钥注入。系统设置页不能展示，settings 表也不能保存历史遗留值，
# 否则管理员页面和数据库备份都会扩大敏感信息暴露面。
ENV_ONLY_SETTING_KEYS = {
    "database.url",
    "redis.url",
    "influxdb.url",
    "influxdb.org",
    "influxdb.bucket",
    "influxdb.token",
    "session.secret",
}


DEFAULT_SETTINGS: dict[str, str] = {
    "scheduler.enabled": "true",
    "scheduler.instance_lock": "locked-by-row-transaction",
    "scheduler.interval_seconds": "1",
    "monitor.enabled": "true",
    "monitor.reconnect_attempts": "3",
    "monitor.watchdog_timeout_seconds": "600",
    "uploads.max_size_mb": "20480",
}

AUDIT_CATEGORIES = {"all", "system", "user", "archive", "file", "task", "env", "node", "other"}


def utc_now() -> str:
    """返回系统本地时区 ISO 时间字符串；保留旧函数名以兼容现有调用点。"""
    return local_now()


def record_audit(
    actor_user_id: int | None,
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
    return parse_datetime_local(value)


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
    created_at = ensure_local_datetime(entry.created_at) or local_datetime()
    return AuditLogInfo(
        id=entry.id,
        actor_user_id=entry.actor_user_id,
        action=entry.action,
        target_type=entry.target_type,
        target_id=entry.target_id,
        ip=entry.ip,
        result=entry.result,
        created_at=created_at.isoformat(),
        detail_json=entry.detail_json or {},
        category=audit_category(entry.action, entry.target_type),
    )


def list_settings(user: UserRecord) -> list[SettingInfo]:
    """返回系统配置项，管理员用于运维面板展示当前配置。"""
    require_permission(user.role, "admin:settings:read")
    with SessionLocal() as db:
        ensure_default_settings(db)
        rows = db.scalars(
            select(Setting)
            .where(~Setting.key.in_(ENV_ONLY_SETTING_KEYS))
            .order_by(Setting.key.asc())
        ).all()
        return [setting_to_info(row) for row in rows]


def update_settings(user: UserRecord, values: dict[str, str]) -> list[SettingInfo]:
    """把系统配置更新到数据库，并记录最后修改人和审计日志。"""
    require_permission(user.role, "admin:settings:write")
    cleaned_values = {
        key.strip(): normalize_setting_value(key.strip(), value)
        for key, value in values.items()
        if key and key.strip() and key.strip() not in ENV_ONLY_SETTING_KEYS
    }
    now = local_datetime()
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
    deleted = db.query(Setting).filter(Setting.key.in_(ENV_ONLY_SETTING_KEYS)).delete(synchronize_session=False)
    if deleted:
        changed = True
    for key, value in default_settings().items():
        if db.get(Setting, key) is not None:
            continue
        db.add(Setting(key=key, value=value))
        changed = True
    if changed:
        db.commit()


def default_settings() -> dict[str, str]:
    """从运行时配置生成完整默认设置清单；只新增缺失行，避免覆盖管理员已经保存的值。"""
    settings = get_settings()
    values = dict(DEFAULT_SETTINGS)
    for item in fields(settings):
        key = SETTING_KEY_ALIASES.get(item.name, item.name.replace("_", "."))
        if key in ENV_ONLY_SETTING_KEYS:
            continue
        values.setdefault(key, setting_value_to_text(getattr(settings, item.name)))
    return values


def setting_value_to_text(value: Any) -> str:
    """把布尔、列表和普通值统一转换成 settings 表可保存、前端可编辑的字符串。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return ",".join(str(item) for item in value)
    return str(value)


def normalize_setting_value(key: str, value: Any) -> str:
    """按配置类型收敛管理员输入，避免布尔大小写或数字空格影响 worker 读取。"""
    text = str(value).strip()
    value_type = SETTING_VALUE_TYPES.get(key, "string")
    if value_type == "boolean":
        return "true" if text.lower() in {"1", "true", "yes", "on", "开启"} else "false"
    if value_type == "integer":
        try:
            return str(int(text))
        except (TypeError, ValueError):
            return text
    if value_type == "number":
        try:
            return str(float(text))
        except (TypeError, ValueError):
            return text
    return text


def setting_to_info(row: Setting) -> SettingInfo:
    """把设置表记录转换为 API 响应，隐藏数据库对象并统一时间格式。"""
    updated_at = row.updated_at
    updated_at = ensure_local_datetime(updated_at)
    return SettingInfo(
        key=row.key,
        value=row.value,
        description=SETTING_DESCRIPTIONS.get(row.key, "自定义系统设置项；请确认业务代码会读取该键后再修改。"),
        value_type=SETTING_VALUE_TYPES.get(row.key, "string"),
        options=SETTING_OPTIONS.get(row.key, []),
        updated_by=row.updated_by,
        updated_at=updated_at.isoformat() if updated_at else None,
    )
