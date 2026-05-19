from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import count

from app.core.errors import unauthorized
from app.core.rbac import Role, list_permissions
from app.core.security import create_session_token, hash_password, verify_password, verify_session_token
from app.schemas.auth import LoginResult, LoginSessionInfo, PublicUser

SESSION_STALE_MINUTES = 30


@dataclass(frozen=True)
class UserRecord:
    """MVP 阶段的内存用户记录，后续会替换为数据库模型。"""

    id: int
    username: str
    real_name: str
    role: Role
    state: str
    password_hash: str


@dataclass(frozen=True)
class LoginSessionRecord:
    """MVP 阶段的内存登录会话记录，用于登录设备/IP 追踪。"""

    id: int
    user_id: int
    token: str
    login_ip: str
    user_agent: str
    login_device: str
    device_id: str
    login_time: str
    last_seen_at: str
    logout_time: str | None = None
    revoked_at: str | None = None


DEMO_USERS: list[UserRecord] = [
    UserRecord(
        id=1,
        username="admin",
        real_name="System Admin",
        role=Role.ADMIN,
        state="enabled",
        password_hash="240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9",
    ),
]
_SESSION_ID = count(1)
LOGIN_SESSIONS: list[LoginSessionRecord] = []


def find_user(identity: str) -> UserRecord | None:
    """按用户名、用户 ID 或真实姓名查找用户，保持与需求里的登录入口一致。"""
    lowered = identity.strip().lower()
    for user in DEMO_USERS:
        if lowered in {user.username.lower(), str(user.id), user.real_name.lower()}:
            return user
    return None


def find_user_by_id(user_id: int) -> UserRecord | None:
    """按平台用户 ID 查找内存用户，供路径解析等仍保存 user_id 的业务对象使用。"""
    for user in DEMO_USERS:
        if user.id == user_id:
            return user
    return None


def register_user_record(user: UserRecord) -> None:
    """把新建用户加入演示用户仓库，使其在内存模式下也能登录。"""
    DEMO_USERS.append(user)


def update_user_record(user: UserRecord, **changes: object) -> UserRecord:
    """更新内存用户记录并返回新对象；dataclass 保持不可变，避免外部持有引用被静默改写。"""
    updated = replace(user, **changes)
    for index, record in enumerate(DEMO_USERS):
        if record.id == user.id:
            DEMO_USERS[index] = updated
            return updated
    raise unauthorized("invalid user")


def remove_user_record(user_id: int) -> UserRecord | None:
    """从内存用户仓库移除指定用户；真实数据库接入后会替换为事务删除。"""
    for index, user in enumerate(DEMO_USERS):
        if user.id == user_id:
            return DEMO_USERS.pop(index)
    return None


def authenticate_user(identity: str, password: str, login_ip: str = "unknown", user_agent: str = "", device_id: str = "") -> LoginResult:
    """校验用户身份和密码，并返回可直接展示在登录页的明确失败原因。"""
    user = find_user(identity)
    if user is None:
        raise unauthorized("用户不存在")
    if user.state != "enabled":
        raise unauthorized("账号已停用，请联系管理员")
    if not verify_password(password, user.password_hash):
        raise unauthorized("密码错误")
    token = create_session_token(user.username)
    record_login_session(user, token, login_ip, user_agent, device_id)
    return LoginResult(access_token=token, user=build_public_user(user))


def get_user_by_token(token: str) -> UserRecord:
    """根据演示令牌解析当前用户，并刷新在线会话最后活跃时间。"""
    parts = token.split(".")
    if len(parts) not in {3, 4}:
        raise unauthorized("invalid token")
    username = parts[1]
    user = find_user(username)
    if user is None or not verify_session_token(token, user.username):
        raise unauthorized("invalid token")
    if user.state != "enabled":
        raise unauthorized("账号已停用，请重新登录")
    session = find_session_by_token(token)
    if session is None:
        # 服务刚重启且内存会话为空时兼容旧 token；只要已经有会话记录，就拒绝孤儿 token。
        if LOGIN_SESSIONS:
            raise unauthorized("session offline")
    elif not session_is_active(session):
        raise unauthorized("session offline")
    touch_login_session(token)
    return user


def build_public_user(user: UserRecord) -> PublicUser:
    """把内部用户记录转换为可返回给前端的安全用户模型。"""
    return PublicUser(
        id=user.id,
        username=user.username,
        real_name=user.real_name,
        role=user.role.value,
        state=user.state,
        permissions=list_permissions(user.role),
    )


def update_current_user_profile(user: UserRecord, real_name: str) -> UserRecord:
    """更新当前用户可自助维护的资料字段，不允许借此修改用户名、角色或状态。"""
    return update_user_record(user, real_name=real_name)


def change_current_user_password(user: UserRecord, current_password: str, new_password: str) -> None:
    """校验旧密码后修改当前用户密码。"""
    if not verify_password(current_password, user.password_hash):
        raise unauthorized("current password is invalid")
    update_user_record(user, password_hash=hash_password(new_password))


def record_login_session(user: UserRecord, token: str, login_ip: str, user_agent: str, device_id: str = "") -> LoginSessionRecord:
    """记录登录会话；允许多设备同时在线，仅合并同一客户端指纹的重复登录。"""
    now = utc_now()
    normalized_ip = login_ip or "unknown"
    normalized_agent = user_agent or ""
    device = describe_device(normalized_agent)
    normalized_device_id = normalize_device_id(device_id)
    reusable = find_mergeable_session(user.id, normalized_ip, normalized_agent, normalized_device_id)
    if reusable is not None:
        updated = update_session_by_id(
            reusable.id,
            token=token,
            login_ip=normalized_ip,
            user_agent=normalized_agent,
            login_device=device,
            device_id=normalized_device_id,
            login_time=now,
            last_seen_at=now,
            logout_time=None,
            revoked_at=None,
        )
        return updated or reusable
    session = LoginSessionRecord(
        id=next(_SESSION_ID),
        user_id=user.id,
        token=token,
        login_ip=normalized_ip,
        user_agent=normalized_agent,
        login_device=device,
        device_id=normalized_device_id,
        login_time=now,
        last_seen_at=now,
    )
    LOGIN_SESSIONS.append(session)
    return session


def find_mergeable_session(user_id: int, login_ip: str, user_agent: str, device_id: str) -> LoginSessionRecord | None:
    """查找可合并的在线会话。

    NebulaGrid 允许同一用户在多台设备上同时在线，因此不能仅凭同 IP 或粗略设备名合并，
    否则同一实验室/NAT 下的多台电脑会互相覆盖。优先使用前端持久化的设备 ID；
    缺失设备 ID 时才退回到 IP + User-Agent 的组合指纹。
    """
    for session in reversed(LOGIN_SESSIONS):
        if session.user_id != user_id or not session_is_active(session):
            continue
        if device_id and session.device_id == device_id:
            return session
        same_network_client = not device_id and session.device_id == "" and login_ip != "unknown" and session.login_ip == login_ip and bool(user_agent) and session.user_agent == user_agent
        if same_network_client:
            return session
    return None


def list_login_sessions(user: UserRecord, current_token: str | None = None) -> list[LoginSessionInfo]:
    """返回当前用户的登录设备列表。

    这里展示的是“设备视图”，不是原始登录流水。历史上同一浏览器多次登录会留下多条
    token/session 记录；若直接返回这些记录，前端会出现同一设备重复多行的问题。
    因此先按设备指纹聚合，再只返回每组最新/当前的代表会话。
    """
    sessions = [session for session in LOGIN_SESSIONS if session.user_id == user.id]
    grouped = aggregate_login_sessions(sessions, current_token)
    return [build_session_info(session, current_token) for session in grouped]


def logout_session(token: str) -> None:
    """标记当前 token 对应会话已退出；无记录时保持幂等。"""
    update_session(token, logout_time=utc_now())


def revoke_login_session(user: UserRecord, session_id: int, current_token: str | None = None) -> LoginSessionInfo:
    """手动下线当前用户指定登录设备。

    前端展示的是聚合后的设备行，因此这里也要按同一个设备指纹批量下线，
    避免只下线代表 session 后，同一设备的旧在线 session 又顶上来。
    """
    target = next((session for session in LOGIN_SESSIONS if session.user_id == user.id and session.id == session_id), None)
    if target is None:
        raise unauthorized("session not found")
    now = utc_now()
    updated_target: LoginSessionRecord | None = None
    for session in list(LOGIN_SESSIONS):
        if session.user_id != user.id or not sessions_same_device(target, session):
            continue
        updated = update_session_by_id(session.id, revoked_at=session.revoked_at or now, logout_time=session.logout_time or now)
        if session.id == target.id:
            updated_target = updated
    return build_session_info(updated_target or target, current_token)


def touch_login_session(token: str) -> None:
    """刷新会话最后活跃时间，便于展示在线设备。"""
    update_session(token, last_seen_at=utc_now())


def update_session(token: str, **changes: object) -> LoginSessionRecord | None:
    """按 token 更新内存会话记录，找不到时返回 None。"""
    for index, session in enumerate(LOGIN_SESSIONS):
        if session.token == token:
            updated = replace(session, **changes)
            LOGIN_SESSIONS[index] = updated
            return updated
    return None


def update_session_by_id(session_id: int, **changes: object) -> LoginSessionRecord | None:
    """按会话 ID 更新内存会话记录。"""
    for index, session in enumerate(LOGIN_SESSIONS):
        if session.id == session_id:
            updated = replace(session, **changes)
            LOGIN_SESSIONS[index] = updated
            return updated
    return None


def find_session_by_token(token: str) -> LoginSessionRecord | None:
    """按 token 查找会话。"""
    for session in LOGIN_SESSIONS:
        if session.token == token:
            return session
    return None


def aggregate_login_sessions(sessions: list[LoginSessionRecord], current_token: str | None = None) -> list[LoginSessionRecord]:
    """把原始登录流水折叠成设备列表。

    聚合规则兼顾两类情况：
    1. 新版前端会持久化 X-NG-Device-Id，同一浏览器重复登录按 device_id 合并；
    2. 旧记录或浏览器清理 localStorage 后可能没有稳定 device_id，此时退回到 IP + User-Agent 合并。

    每组优先展示当前会话，其次展示在线会话，再按最后活跃时间选最新记录。
    """
    ordered = sorted(
        sessions,
        key=lambda item: (
            item.token == current_token,
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
    """把内部会话记录转换成前端展示模型，并计算在线/离线状态。"""
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
        current=current_token == session.token,
    )


def session_is_active(session: LoginSessionRecord) -> bool:
    """会话未退出、未被手动下线且最近有活跃心跳时认为在线。"""
    if session.logout_time is not None or session.revoked_at is not None:
        return False
    seen = parse_datetime(session.last_seen_at)
    if seen is None:
        return False
    return utc_datetime() - seen <= timedelta(minutes=SESSION_STALE_MINUTES)


def parse_datetime(value: str) -> datetime | None:
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


def utc_datetime() -> datetime:
    return datetime.now(timezone.utc)


def utc_now() -> str:
    """返回 UTC ISO 时间字符串，避免 auth_service 依赖审计服务造成循环导入。"""
    return utc_datetime().isoformat()
