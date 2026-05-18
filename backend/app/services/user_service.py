from itertools import count

from app.core.config import get_settings
from app.core.errors import forbidden, validation_error
from app.core.rbac import Role, require_permission
from app.core.security import hash_password
from app.schemas.users import UserCreateRequest, UserInfo
from app.services.audit_service import record_audit, utc_now
from app.services.auth_service import DEMO_USERS, UserRecord, register_user_record

_USER_ID = count(2)


def list_users(user: UserRecord) -> list[UserInfo]:
    """返回用户列表，导师和管理员可通过服务层查看受控范围。"""
    require_permission(user.role, "users:read")
    return [to_user_info(record) for record in DEMO_USERS]


def create_user(user: UserRecord, payload: UserCreateRequest) -> UserInfo:
    """创建平台用户，并强制导师只能创建学生账号。"""
    if any(record.username == payload.username for record in DEMO_USERS):
        raise validation_error("username already exists")
    target_role = Role(payload.role)
    if user.role == Role.MENTOR and target_role != Role.STUDENT:
        raise forbidden("mentor can only create student users")
    if target_role == Role.STUDENT:
        require_permission(user.role, "users:create_student")
    else:
        require_permission(user.role, "users:create")
    record = UserRecord(
        id=next(_USER_ID),
        username=payload.username,
        real_name=payload.real_name,
        role=target_role,
        state=payload.state,
        password_hash=hash_password(payload.password),
    )
    register_user_record(record)
    record_audit(user.id, "user.create", "user", str(record.id), detail_json={"role": payload.role})
    return to_user_info(record)


def to_user_info(record: UserRecord) -> UserInfo:
    """把内部用户记录转换为用户管理接口的安全响应模型。"""
    settings = get_settings()
    return UserInfo(
        id=record.id,
        username=record.username,
        real_name=record.real_name,
        role=record.role.value,
        state=record.state,
        home_path=f"{settings.user_home_root}/{record.id}",
        linux_account_name=record.username,
        created_at=utc_now(),
    )
