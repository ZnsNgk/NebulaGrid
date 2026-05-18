from dataclasses import dataclass

from app.core.errors import unauthorized
from app.core.rbac import Role, list_permissions
from app.core.security import create_session_token, verify_password, verify_session_token
from app.schemas.auth import LoginResult, PublicUser


@dataclass(frozen=True)
class UserRecord:
    """MVP 阶段的内存用户记录，后续会替换为数据库模型。"""

    id: int
    username: str
    real_name: str
    role: Role
    state: str
    password_hash: str


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


def find_user(identity: str) -> UserRecord | None:
    """按用户名、用户 ID 或真实姓名查找用户，保持与需求里的登录入口一致。"""
    lowered = identity.strip().lower()
    for user in DEMO_USERS:
        if lowered in {user.username.lower(), str(user.id), user.real_name.lower()}:
            return user
    return None


def register_user_record(user: UserRecord) -> None:
    """把新建用户加入演示用户仓库，使其在内存模式下也能登录。"""
    DEMO_USERS.append(user)


def authenticate_user(identity: str, password: str) -> LoginResult:
    """校验用户身份和密码，失败时统一返回未认证错误避免泄露账号存在性。"""
    user = find_user(identity)
    if user is None or user.state != "enabled" or not verify_password(password, user.password_hash):
        raise unauthorized("invalid identity or password")
    token = create_session_token(user.username)
    return LoginResult(access_token=token, user=build_public_user(user))


def get_user_by_token(token: str) -> UserRecord:
    """根据演示令牌解析当前用户，后续会替换为 JWT/session 查询。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise unauthorized("invalid token")
    username = parts[1]
    user = find_user(username)
    if user is None or not verify_session_token(token, user.username):
        raise unauthorized("invalid token")
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
