from dataclasses import dataclass, replace
from itertools import count

from app.core.errors import unauthorized
from app.core.rbac import Role, list_permissions
from app.core.security import create_session_token, hash_password, verify_password, verify_session_token
from app.schemas.auth import LoginResult, LoginSessionInfo, PublicUser


@dataclass(frozen=True)
class UserRecord:
    """MVP 阶段的内存用户记录，后续会替换为数据库模型。"""

    id: int
    username: str
    real_name: str
    role: Role
    state: str
    password_hash: str
    avatar: str | None = None


@dataclass(frozen=True)
class LoginSessionRecord:
    """MVP 阶段的内存登录会话记录，后续会替换为 login_sessions 数据表。"""

    id: int
    user_id: int
    token: str
    login_ip: str
    user_agent: str
    login_device: str
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


def authenticate_user(identity: str, password: str, login_ip: str = "unknown", user_agent: str = "") -> LoginResult:
    """校验用户身份和密码，失败时统一返回未认证错误避免泄露账号存在性。"""
    user = find_user(identity)
    if user is None or user.state != "enabled" or not verify_password(password, user.password_hash):
        raise unauthorized("invalid identity or password")
    token = create_session_token(user.username)
    record_login_session(user, token, login_ip, user_agent)
    return LoginResult(access_token=token, user=build_public_user(user))


def get_user_by_token(token: str) -> UserRecord:
    """根据演示令牌解析当前用户，后续会替换为 JWT/session 查询。"""
    parts = token.split(".")
    if len(parts) not in {3, 4}:
        raise unauthorized("invalid token")
    username = parts[1]
    user = find_user(username)
    if user is None or not verify_session_token(token, user.username):
        raise unauthorized("invalid token")
    session = find_session_by_token(token)
    if session is not None and (session.logout_time is not None or session.revoked_at is not None):
        raise unauthorized("session revoked")
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
        avatar=user.avatar,
    )


def update_current_user_profile(user: UserRecord, real_name: str, avatar: str | None) -> UserRecord:
    """更新当前用户可自助维护的资料字段，不允许借此修改用户名、角色或状态。"""
    return update_user_record(user, real_name=real_name, avatar=avatar)


def change_current_user_password(user: UserRecord, current_password: str, new_password: str) -> None:
    """校验旧密码后修改当前用户密码，并吊销除当前请求外的历史会话由调用方决定。"""
    if not verify_password(current_password, user.password_hash):
        raise unauthorized("current password is invalid")
    update_user_record(user, password_hash=hash_password(new_password))


def record_login_session(user: UserRecord, token: str, login_ip: str, user_agent: str) -> LoginSessionRecord:
    """记录一次登录会话，设备名从 User-Agent 中提取，便于用户识别来源。"""
    now = utc_now()
    session = LoginSessionRecord(
        id=next(_SESSION_ID),
        user_id=user.id,
        token=token,
        login_ip=login_ip or "unknown",
        user_agent=user_agent or "",
        login_device=describe_device(user_agent),
        login_time=now,
        last_seen_at=now,
    )
    LOGIN_SESSIONS.append(session)
    return session


def list_login_sessions(user: UserRecord, current_token: str | None = None) -> list[LoginSessionInfo]:
    """返回当前用户的登录设备列表，包含历史退出和已撤销会话。"""
    return [
        build_session_info(session, current_token)
        for session in reversed(LOGIN_SESSIONS)
        if session.user_id == user.id
    ]


def logout_session(token: str) -> None:
    """标记当前 token 对应会话已退出；无记录时保持幂等。"""
    update_session(token, logout_time=utc_now())


def revoke_login_session(user: UserRecord, session_id: int, current_token: str | None = None) -> LoginSessionInfo:
    """撤销当前用户指定登录设备；当前会话也允许撤销，前端会随后清理本地 token。"""
    for session in LOGIN_SESSIONS:
        if session.user_id == user.id and session.id == session_id:
            updated = update_session(session.token, revoked_at=utc_now(), logout_time=session.logout_time or utc_now())
            return build_session_info(updated or session, current_token)
    raise unauthorized("session not found")


def touch_login_session(token: str) -> None:
    """刷新会话最后活跃时间，便于展示在线设备。"""
    update_session(token, last_seen_at=utc_now())


def update_session(token: str, **changes: object) -> LoginSessionRecord | None:
    """更新内存会话记录，找不到时返回 None 以兼容旧 token。"""
    for index, session in enumerate(LOGIN_SESSIONS):
        if session.token == token:
            updated = replace(session, **changes)
            LOGIN_SESSIONS[index] = updated
            return updated
    return None


def find_session_by_token(token: str) -> LoginSessionRecord | None:
    """按 token 查找会话；兼容早期没有服务端会话记录的演示 token。"""
    for session in LOGIN_SESSIONS:
        if session.token == token:
            return session
    return None


def build_session_info(session: LoginSessionRecord, current_token: str | None = None) -> LoginSessionInfo:
    """把内部会话记录转换成前端展示模型，并计算在线/离线状态。"""
    active = session.logout_time is None and session.revoked_at is None
    return LoginSessionInfo(
        id=session.id,
        login_ip=session.login_ip,
        login_device=session.login_device,
        user_agent=session.user_agent,
        login_time=session.login_time,
        last_seen_at=session.last_seen_at,
        logout_time=session.logout_time,
        revoked_at=session.revoked_at,
        state="online" if active else "offline",
        current=current_token == session.token,
    )


def describe_device(user_agent: str) -> str:
    """从 User-Agent 提取一个可读设备摘要；为空时返回 unknown。"""
    if not user_agent:
        return "unknown device"
    lowered = user_agent.lower()
    browser = "Chrome" if "chrome" in lowered else "Firefox" if "firefox" in lowered else "Safari" if "safari" in lowered else "Browser"
    system = "Windows" if "windows" in lowered else "macOS" if "mac os" in lowered else "Linux" if "linux" in lowered else "Device"
    return f"{browser} on {system}"


def utc_now() -> str:
    """返回 UTC ISO 时间字符串，避免 auth_service 依赖审计服务造成循环导入。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
