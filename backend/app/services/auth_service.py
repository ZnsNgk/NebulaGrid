from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import unauthorized
from app.core.rbac import Role, list_permissions, require_permission
from app.core.security import create_session_token, hash_password, verify_password, verify_session_token
from app.db.models import LoginSession, User, UserSupervisor
from app.db.session import SessionLocal
from app.schemas.auth import AdminOnlineUserInfo, AdminUserLoginSessions, LoginResult, LoginSessionInfo, PublicUser
from app.services.linux_account_service import home_path_for_user, linux_account_for_role

SESSION_STALE_MINUTES = 30


@dataclass(frozen=True)
class UserRecord:
    """服务层轻量用户对象，避免业务代码直接持有可变 ORM 实例。"""

    id: int
    username: str
    real_name: str
    role: Role
    state: str
    password_hash: str
    supervisor_ids: tuple[int, ...] = ()
    home_path: str = ""
    linux_account_name: str | None = None
    linux_uid: int | None = None
    linux_gid: int | None = None
    created_at: str = ""


@dataclass(frozen=True)
class LoginSessionRecord:
    """登录设备流水对象；数据库只保存 token_hash，不落原始登录令牌。"""

    id: int
    user_id: int
    token_hash: str
    login_ip: str
    user_agent: str
    login_device: str
    device_id: str
    login_time: str
    last_seen_at: str
    logout_time: str | None = None
    revoked_at: str | None = None


def find_user(identity: str) -> UserRecord | None:
    """按用户名、用户 ID 或真实姓名查找用户，用于登录入口。"""
    with SessionLocal() as db:
        user = find_user_model(db, identity)
        return user_model_to_record(user, db) if user is not None else None


def find_user_by_id(user_id: int) -> UserRecord | None:
    """按平台用户 ID 查找用户，供任务和路径等业务对象使用。"""
    with SessionLocal() as db:
        user = db.get(User, user_id)
        return user_model_to_record(user, db) if user is not None else None


def register_user_record(user: UserRecord) -> None:
    """兼容旧调用：把轻量用户对象写入 users 表。"""
    with SessionLocal() as db:
        if db.get(User, user.id) is not None:
            return
        db.add(
            User(
                id=user.id,
                username=user.username,
                real_name=user.real_name,
                role=user.role.value,
                password_hash=user.password_hash,
                state=user.state,
                home_path=user.home_path or home_path_for_user(user.username, user.role.value),
                linux_account_name=user.linux_account_name or linux_account_for_role(user.username, user.role.value),
                linux_uid=user.linux_uid,
                linux_gid=user.linux_gid,
            )
        )
        for supervisor_id in user.supervisor_ids:
            db.add(UserSupervisor(student_id=user.id, supervisor_id=supervisor_id))
        db.commit()


def update_user_record(user: UserRecord, **changes: object) -> UserRecord:
    """兼容旧调用：在数据库中更新用户字段并返回新的轻量对象。"""
    with SessionLocal() as db:
        model = db.get(User, user.id)
        if model is None:
            raise unauthorized("invalid user")
        if "role" in changes and isinstance(changes["role"], Role):
            changes["role"] = changes["role"].value
        supervisor_ids = changes.pop("supervisor_ids", None)
        for key, value in changes.items():
            if hasattr(model, key):
                setattr(model, key, value)
        if "username" in changes or "role" in changes:
            model.home_path = home_path_for_user(model.username, model.role)
            model.linux_account_name = linux_account_for_role(model.username, model.role)
        if supervisor_ids is not None:
            replace_supervisors(db, model.id, tuple(int(item) for item in supervisor_ids))
        db.commit()
        db.refresh(model)
        return user_model_to_record(model, db)


def remove_user_record(user_id: int) -> UserRecord | None:
    """兼容旧调用：从数据库移除指定用户。"""
    with SessionLocal() as db:
        model = db.get(User, user_id)
        if model is None:
            return None
        record = user_model_to_record(model, db)
        db.query(UserSupervisor).filter(
            or_(UserSupervisor.student_id == user_id, UserSupervisor.supervisor_id == user_id)
        ).delete(synchronize_session=False)
        db.delete(model)
        db.commit()
        return record


def authenticate_user(identity: str, password: str, login_ip: str = "unknown", user_agent: str = "", device_id: str = "") -> LoginResult:
    """校验用户身份和密码，并把登录设备流水写入 login_sessions 表。"""
    with SessionLocal() as db:
        user_model = find_user_model(db, identity)
        if user_model is None:
            raise unauthorized("用户不存在")
        user = user_model_to_record(user_model, db)
        if user.state != "enabled":
            raise unauthorized("账号已停用，请联系管理员")
        if not verify_password(password, user.password_hash):
            raise unauthorized("密码错误")
        token = create_session_token(user.username)
        record_login_session_model(db, user, token, login_ip, user_agent, device_id)
        db.commit()
        return LoginResult(access_token=token, user=build_public_user(user))


def get_user_by_token(token: str) -> UserRecord:
    """根据 Bearer token 解析当前用户，并刷新对应会话的最后活跃时间。"""
    parts = token.split(".")
    if len(parts) not in {3, 4}:
        raise unauthorized("invalid token")
    username = parts[1]
    with SessionLocal() as db:
        user_model = find_user_model(db, username)
        if user_model is None or not verify_session_token(token, user_model.username):
            raise unauthorized("invalid token")
        user = user_model_to_record(user_model, db)
        if user.state != "enabled":
            raise unauthorized("账号已停用，请重新登录")
        session = find_session_model_by_token(db, token)
        if session is None:
            # 兼容从旧内存会话升级后的存量 token；一旦库里已有会话，就拒绝孤儿 token。
            session_count = db.scalar(select(func.count()).select_from(LoginSession)) or 0
            if session_count:
                raise unauthorized("session offline")
        elif not session_model_is_active(session):
            raise unauthorized("session offline")
        else:
            session.last_seen_at = utc_datetime()
            db.commit()
        return user


def build_public_user(user: UserRecord) -> PublicUser:
    """把内部用户对象转换为可返回给前端的安全用户模型。"""
    return PublicUser(
        id=user.id,
        username=user.username,
        real_name=user.real_name,
        role=user.role.value,
        state=user.state,
        permissions=list_permissions(user.role),
    )


def update_current_user_profile(user: UserRecord, real_name: str) -> UserRecord:
    """更新当前用户可自助维护的资料字段。"""
    return update_user_record(user, real_name=real_name)


def change_current_user_password(user: UserRecord, current_password: str, new_password: str) -> None:
    """校验旧密码后修改当前用户密码。"""
    if not verify_password(current_password, user.password_hash):
        raise unauthorized("current password is invalid")
    update_user_record(user, password_hash=hash_password(new_password))


def record_login_session(user: UserRecord, token: str, login_ip: str, user_agent: str, device_id: str = "") -> LoginSessionRecord:
    """记录登录会话，供外部需要手动签发 token 的流程复用。"""
    with SessionLocal() as db:
        record = record_login_session_model(db, user, token, login_ip, user_agent, device_id)
        db.commit()
        return record


def record_login_session_model(
    db: Session,
    user: UserRecord,
    token: str,
    login_ip: str,
    user_agent: str,
    device_id: str = "",
) -> LoginSessionRecord:
    """在同一事务内写入或合并登录设备流水。"""
    now = utc_datetime()
    normalized_ip = login_ip or "unknown"
    normalized_agent = normalize_user_agent(user_agent)
    device = describe_device(normalized_agent)
    normalized_device_id = normalize_device_id(device_id)
    reusable = find_mergeable_session_model(db, user.id, normalized_ip, normalized_agent, normalized_device_id)
    if reusable is not None:
        reusable.token_hash = hash_session_token(token)
        reusable.ip = normalized_ip
        reusable.user_agent = normalized_agent
        reusable.login_device = device
        reusable.device_id = normalized_device_id
        reusable.created_at = now
        reusable.last_seen_at = now
        reusable.logout_at = None
        reusable.revoked_at = None
        db.flush()
        return session_model_to_record(reusable)
    session = LoginSession(
        user_id=user.id,
        token_hash=hash_session_token(token),
        ip=normalized_ip,
        user_agent=normalized_agent,
        login_device=device,
        device_id=normalized_device_id,
        last_seen_at=now,
    )
    session.created_at = now
    db.add(session)
    db.flush()
    return session_model_to_record(session)


def find_mergeable_session_model(db: Session, user_id: int, login_ip: str, user_agent: str, device_id: str) -> LoginSession | None:
    """查找同用户同设备的活跃会话，避免重复登录刷出多行设备。"""
    sessions = db.scalars(
        select(LoginSession)
        .where(LoginSession.user_id == user_id)
        .order_by(LoginSession.id.desc())
    ).all()
    for session in sessions:
        record = session_model_to_record(session)
        if not session_is_active(record):
            continue
        if device_id and normalize_device_id(session.device_id) == device_id:
            return session
        same_network_client = (
            not device_id
            and normalize_device_id(session.device_id) == ""
            and login_ip != "unknown"
            and (session.ip or "") == login_ip
            and bool(user_agent)
            and normalize_user_agent(session.user_agent) == user_agent
        )
        if same_network_client:
            return session
    return None


def list_login_sessions(user: UserRecord, current_token: str | None = None) -> list[LoginSessionInfo]:
    """返回当前用户的登录设备列表。"""
    with SessionLocal() as db:
        sessions = [
            session_model_to_record(session)
            for session in db.scalars(
                select(LoginSession)
                .where(LoginSession.user_id == user.id)
                .order_by(LoginSession.id.desc())
            ).all()
        ]
    grouped = aggregate_login_sessions(sessions, current_token)
    return [build_session_info(session, current_token) for session in grouped]


def list_admin_online_users(actor: UserRecord) -> list[AdminOnlineUserInfo]:
    """管理员登录管理：查看当前在线用户摘要。"""
    require_permission(actor.role, "admin:login:read")
    with SessionLocal() as db:
        users = db.scalars(select(User).order_by(User.id)).all()
        online_sessions = [
            session_model_to_record(session)
            for session in db.scalars(select(LoginSession).order_by(LoginSession.id.desc())).all()
            if session_model_is_active(session)
        ]
        result: list[AdminOnlineUserInfo] = []
        for user_model in users:
            user = user_model_to_record(user_model, db)
            user_sessions = [session for session in online_sessions if session.user_id == user.id]
            if not user_sessions:
                continue
            last_seen = max(user_sessions, key=lambda item: parse_datetime(item.last_seen_at) or datetime.min.replace(tzinfo=timezone.utc)).last_seen_at
            login_ips = sorted({session.login_ip for session in user_sessions if session.login_ip})
            login_devices = sorted({session.login_device for session in user_sessions if session.login_device})
            result.append(AdminOnlineUserInfo(
                id=user.id,
                username=user.username,
                real_name=user.real_name,
                role=user.role.value,
                state=user.state,
                online_sessions=len(aggregate_login_sessions(user_sessions)),
                login_ips=login_ips,
                login_devices=login_devices,
                last_seen_at=last_seen,
            ))
        return result


def list_admin_user_login_sessions(
    actor: UserRecord,
    user_id: int | None = None,
    keyword: str | None = None,
    current_token: str | None = None,
) -> list[AdminUserLoginSessions]:
    """管理员登录管理：查看指定用户的登录设备、IP 和在线状态。"""
    require_permission(actor.role, "admin:login:read")
    with SessionLocal() as db:
        users = filter_user_models_for_login_management(db, user_id=user_id, keyword=keyword)
        items: list[AdminUserLoginSessions] = []
        for user_model in users:
            user = user_model_to_record(user_model, db)
            sessions = [
                session_model_to_record(session)
                for session in db.scalars(
                    select(LoginSession)
                    .where(LoginSession.user_id == user.id)
                    .order_by(LoginSession.id.desc())
                ).all()
            ]
            grouped = aggregate_login_sessions(sessions, current_token)
            session_infos = [build_session_info(session, current_token) for session in grouped]
            items.append(AdminUserLoginSessions(
                id=user.id,
                username=user.username,
                real_name=user.real_name,
                role=user.role.value,
                state=user.state,
                sessions=session_infos,
            ))
        return items


def offline_login_session_as_admin(actor: UserRecord, session_id: int, current_token: str | None = None) -> LoginSessionInfo:
    """管理员手动下线任意用户的一个登录设备。"""
    require_permission(actor.role, "admin:login:write")
    with SessionLocal() as db:
        target = db.get(LoginSession, session_id)
        if target is None:
            raise unauthorized("session not found")
        target_record = session_model_to_record(target)
        updated_target: LoginSessionRecord | None = None
        now = utc_datetime()
        sessions = db.scalars(select(LoginSession).where(LoginSession.user_id == target.user_id)).all()
        for session in sessions:
            session_record = session_model_to_record(session)
            if not sessions_same_device(target_record, session_record):
                continue
            session.revoked_at = session.revoked_at or now
            session.logout_at = session.logout_at or now
            if session.id == target.id:
                updated_target = session_model_to_record(session)
        db.commit()
        return build_session_info(updated_target or target_record, current_token)


def filter_users_for_login_management(user_id: int | None = None, keyword: str | None = None) -> list[UserRecord]:
    """按统一识别码或关键词筛选登录管理目标用户。"""
    with SessionLocal() as db:
        return [user_model_to_record(user, db) for user in filter_user_models_for_login_management(db, user_id, keyword)]


def filter_user_models_for_login_management(db: Session, user_id: int | None = None, keyword: str | None = None) -> list[User]:
    """在数据库内筛选登录管理目标用户。"""
    if user_id is not None:
        user = db.get(User, user_id)
        return [user] if user is not None else []
    if keyword and keyword.strip():
        lowered = keyword.strip().lower()
        statement = select(User).where(
            or_(
                func.lower(User.username).contains(lowered),
                func.lower(User.real_name).contains(lowered),
                User.id == int(lowered) if lowered.isdigit() else False,
            )
        ).order_by(User.id)
        return list(db.scalars(statement).all())
    return []


def logout_session(token: str) -> None:
    """标记当前 token 对应会话已退出。"""
    update_session(token, logout_time=utc_now())


def revoke_login_session(user: UserRecord, session_id: int, current_token: str | None = None) -> LoginSessionInfo:
    """手动下线当前用户指定登录设备。"""
    with SessionLocal() as db:
        target = db.get(LoginSession, session_id)
        if target is None or target.user_id != user.id:
            raise unauthorized("session not found")
        target_record = session_model_to_record(target)
        updated_target: LoginSessionRecord | None = None
        now = utc_datetime()
        sessions = db.scalars(select(LoginSession).where(LoginSession.user_id == user.id)).all()
        for session in sessions:
            session_record = session_model_to_record(session)
            if not sessions_same_device(target_record, session_record):
                continue
            session.revoked_at = session.revoked_at or now
            session.logout_at = session.logout_at or now
            if session.id == target.id:
                updated_target = session_model_to_record(session)
        db.commit()
        return build_session_info(updated_target or target_record, current_token)


def touch_login_session(token: str) -> None:
    """刷新会话最后活跃时间。"""
    update_session(token, last_seen_at=utc_now())


def update_session(token: str, **changes: object) -> LoginSessionRecord | None:
    """按 token 更新数据库会话记录。"""
    with SessionLocal() as db:
        session = find_session_model_by_token(db, token)
        if session is None:
            return None
        apply_session_changes(session, changes)
        db.commit()
        return session_model_to_record(session)


def update_session_by_id(session_id: int, **changes: object) -> LoginSessionRecord | None:
    """按会话 ID 更新数据库会话记录。"""
    with SessionLocal() as db:
        session = db.get(LoginSession, session_id)
        if session is None:
            return None
        apply_session_changes(session, changes)
        db.commit()
        return session_model_to_record(session)


def find_session_by_token(token: str) -> LoginSessionRecord | None:
    """按 token 查找会话。"""
    with SessionLocal() as db:
        session = find_session_model_by_token(db, token)
        return session_model_to_record(session) if session is not None else None


def aggregate_login_sessions(sessions: list[LoginSessionRecord], current_token: str | None = None) -> list[LoginSessionRecord]:
    """把原始登录流水折叠成设备列表。"""
    current_hash = hash_session_token(current_token) if current_token else None
    ordered = sorted(
        sessions,
        key=lambda item: (
            item.token_hash == current_hash,
            session_is_active(item),
            parse_datetime(item.last_seen_at) or datetime.min.replace(tzinfo=timezone.utc),
            parse_datetime(item.login_time) or datetime.min.replace(tzinfo=timezone.utc),
            item.id,
        ),
        reverse=True,
    )
    representatives: list[LoginSessionRecord] = []
    for session in ordered:
        if any(sessions_same_device(session, existed) for existed in representatives):
            continue
        representatives.append(session)
    return representatives


def sessions_same_device(left: LoginSessionRecord, right: LoginSessionRecord) -> bool:
    """判断两条登录流水是否属于同一个可展示设备。"""
    if left.user_id != right.user_id:
        return False
    left_device_id = normalize_device_id(left.device_id)
    right_device_id = normalize_device_id(right.device_id)
    if left_device_id and right_device_id and left_device_id == right_device_id:
        return True
    left_ip = (left.login_ip or "").strip()
    right_ip = (right.login_ip or "").strip()
    left_agent = normalize_user_agent(left.user_agent)
    right_agent = normalize_user_agent(right.user_agent)
    if left_ip and right_ip and left_ip != "unknown" and left_ip == right_ip and left_agent and left_agent == right_agent:
        return True
    return False


def normalize_user_agent(value: str | None) -> str:
    """归一化 User-Agent，避免空白差异导致同设备无法合并。"""
    if not value:
        return ""
    return " ".join(value.strip().split())


def build_session_info(session: LoginSessionRecord, current_token: str | None = None) -> LoginSessionInfo:
    """把内部会话记录转换成前端展示模型。"""
    current_hash = hash_session_token(current_token) if current_token else None
    active = session_is_active(session)
    logout_time = session.logout_time
    if not active and logout_time is None and session.revoked_at is None:
        logout_time = session.last_seen_at
    return LoginSessionInfo(
        id=session.id,
        login_ip=session.login_ip,
        login_device=session.login_device,
        user_agent=session.user_agent,
        login_time=session.login_time,
        last_seen_at=session.last_seen_at,
        logout_time=logout_time,
        revoked_at=session.revoked_at,
        state="online" if active else "offline",
        current=current_hash == session.token_hash,
    )


def session_is_active(session: LoginSessionRecord) -> bool:
    """会话未退出、未被手动下线且最近有心跳时视为在线。"""
    if session.logout_time is not None or session.revoked_at is not None:
        return False
    seen = parse_datetime(session.last_seen_at)
    if seen is None:
        return False
    return utc_datetime() - seen <= timedelta(minutes=SESSION_STALE_MINUTES)


def session_model_is_active(session: LoginSession) -> bool:
    """用数据库模型判断会话在线状态。"""
    return session_is_active(session_model_to_record(session))


def parse_datetime(value: str | None) -> datetime | None:
    """解析 ISO 时间字符串，失败时返回 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_device_id(value: str | None) -> str:
    """归一化前端生成的设备 ID；异常或过长输入直接丢弃。"""
    if not value:
        return ""
    normalized = value.strip()
    if len(normalized) > 128:
        return ""
    return normalized


def describe_device(user_agent: str) -> str:
    """从 User-Agent 提取一个可读设备摘要；为空时返回 unknown。"""
    if not user_agent:
        return "unknown device"
    lowered = user_agent.lower()
    if "edg/" in lowered or "edge" in lowered:
        browser = "Edge"
    elif "chrome" in lowered or "chromium" in lowered:
        browser = "Chrome"
    elif "firefox" in lowered:
        browser = "Firefox"
    elif "safari" in lowered:
        browser = "Safari"
    else:
        browser = "Browser"
    if "windows" in lowered:
        system = "Windows"
    elif "iphone" in lowered or "ipad" in lowered:
        system = "iOS"
    elif "android" in lowered:
        system = "Android"
    elif "mac os" in lowered or "macintosh" in lowered:
        system = "macOS"
    elif "linux" in lowered:
        system = "Linux"
    else:
        system = "Device"
    return f"{browser} on {system}"


def find_user_model(db: Session, identity: str) -> User | None:
    """在 users 表中按登录标识查找用户。"""
    lowered = identity.strip().lower()
    conditions: list[Any] = [
        func.lower(User.username) == lowered,
        func.lower(User.real_name) == lowered,
    ]
    if lowered.isdigit():
        conditions.append(User.id == int(lowered))
    return db.scalar(select(User).where(or_(*conditions)))


def user_model_to_record(user: User, db: Session) -> UserRecord:
    """把用户 ORM 模型转换为服务层轻量对象。"""
    supervisor_ids = tuple(
        db.scalars(
            select(UserSupervisor.supervisor_id)
            .where(UserSupervisor.student_id == user.id)
            .order_by(UserSupervisor.supervisor_id)
        ).all()
    )
    return UserRecord(
        id=user.id,
        username=user.username,
        real_name=user.real_name,
        role=Role(user.role),
        state=user.state,
        password_hash=user.password_hash,
        supervisor_ids=supervisor_ids,
        home_path=user.home_path,
        linux_account_name=user.linux_account_name,
        linux_uid=user.linux_uid,
        linux_gid=user.linux_gid,
        created_at=datetime_to_iso(user.created_at),
    )


def replace_supervisors(db: Session, student_id: int, supervisor_ids: tuple[int, ...]) -> None:
    """用关系表整体替换学生导师关系，避免固定 supervisor1/2 字段膨胀。"""
    db.query(UserSupervisor).filter(UserSupervisor.student_id == student_id).delete(synchronize_session=False)
    for supervisor_id in supervisor_ids:
        db.add(UserSupervisor(student_id=student_id, supervisor_id=supervisor_id))


def find_session_model_by_token(db: Session, token: str) -> LoginSession | None:
    """按 token 摘要查找数据库会话。"""
    return db.scalar(select(LoginSession).where(LoginSession.token_hash == hash_session_token(token)))


def session_model_to_record(session: LoginSession) -> LoginSessionRecord:
    """把登录会话 ORM 模型转换为服务层轻量对象。"""
    return LoginSessionRecord(
        id=session.id,
        user_id=session.user_id,
        token_hash=session.token_hash,
        login_ip=session.ip or "unknown",
        user_agent=session.user_agent or "",
        login_device=session.login_device or "unknown device",
        device_id=session.device_id or "",
        login_time=datetime_to_iso(session.created_at),
        last_seen_at=datetime_to_iso(session.last_seen_at),
        logout_time=datetime_to_iso(session.logout_at) if session.logout_at else None,
        revoked_at=datetime_to_iso(session.revoked_at) if session.revoked_at else None,
    )


def apply_session_changes(session: LoginSession, changes: dict[str, object]) -> None:
    """把旧服务层字段名映射到 login_sessions 表字段。"""
    field_map = {
        "token": "token_hash",
        "login_ip": "ip",
        "login_time": "created_at",
        "logout_time": "logout_at",
    }
    datetime_fields = {"created_at", "last_seen_at", "logout_at", "expires_at", "revoked_at"}
    for key, value in changes.items():
        target = field_map.get(key, key)
        if target == "token_hash" and isinstance(value, str):
            value = hash_session_token(value)
        if target in datetime_fields and isinstance(value, str):
            value = parse_datetime(value)
        if hasattr(session, target):
            setattr(session, target, value)


def hash_session_token(token: str | None) -> str:
    """生成会话令牌摘要，数据库不保存原始 token。"""
    if not token:
        return ""
    return sha256(token.encode("utf-8")).hexdigest()


def datetime_to_iso(value: datetime | None) -> str:
    """把数据库时间统一转成带时区的 ISO 字符串。"""
    if value is None:
        return utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def utc_datetime() -> datetime:
    """返回 UTC 当前时间，供数据库和响应模型共用。"""
    return datetime.now(timezone.utc)


def utc_now() -> str:
    """返回 UTC ISO 时间字符串。"""
    return utc_datetime().isoformat()
