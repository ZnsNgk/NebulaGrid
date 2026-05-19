from itertools import count

from app.core.config import get_settings
from app.core.errors import forbidden, not_found, validation_error
from app.core.rbac import Role, require_permission
from app.core.security import hash_password
from app.schemas.users import UserCreateRequest, UserInfo, UserUpdateRequest, UserPasswordResetRequest
from app.services.audit_service import record_audit, utc_now
from app.services.auth_service import DEMO_USERS, UserRecord, register_user_record, remove_user_record, update_user_record
from app.services.linux_account_service import (
    create_child_account,
    delete_child_account,
    home_path_for_user,
    linux_account_for_role,
)

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
    account_plan = create_child_account(record.username, record.id, record.role.value, password=payload.password)
    register_user_record(record)
    record_audit(
        user.id,
        "user.create",
        "user",
        str(record.id),
        detail_json={
            "role": payload.role,
            "linux_account_name": account_plan.account_name,
            "linux_account_executed": account_plan.executed,
            "home_path": account_plan.home_path,
        },
    )
    return to_user_info(record)


def update_user(user: UserRecord, user_id: int, payload: UserUpdateRequest) -> UserInfo:
    """更新平台用户基础资料、角色和启停状态；管理员可改任意用户，导师只能改学生。"""
    require_permission(user.role, "users:read")
    target = next((record for record in DEMO_USERS if record.id == user_id), None)
    if target is None:
        raise not_found("user not found")
    if user.role == Role.MENTOR and target.role != Role.STUDENT:
        raise forbidden("mentor can only manage student users")
    if user.role != Role.ADMIN and user.id != target.id and target.role != Role.STUDENT:
        raise forbidden("user update not allowed")
    data = payload.model_dump(exclude_unset=True)
    if "role" in data and data["role"] is not None:
        next_role = Role(data["role"])
        if user.role != Role.ADMIN:
            raise forbidden("only admin can change roles")
        if target.role == Role.ADMIN and next_role != Role.ADMIN and count_admin_users() <= 1:
            raise forbidden("last admin user cannot be downgraded")
        data["role"] = next_role
    if "state" in data and data["state"] is not None and target.role == Role.ADMIN and data["state"] != "enabled" and count_admin_users() <= 1:
        raise forbidden("last admin user cannot be disabled")
    updated = update_user_record(target, **data)
    record_audit(user.id, "user.update", "user", str(updated.id), detail_json={k: str(v) for k, v in data.items()})
    return to_user_info(updated)


def reset_user_password(user: UserRecord, user_id: int, payload: UserPasswordResetRequest) -> UserInfo:
    """管理员重置任意账号密码；导师可重置自己学生账号密码。"""
    require_permission(user.role, "users:read")
    target = next((record for record in DEMO_USERS if record.id == user_id), None)
    if target is None:
        raise not_found("user not found")
    if user.role == Role.MENTOR and target.role != Role.STUDENT:
        raise forbidden("mentor can only reset student passwords")
    if user.role not in {Role.ADMIN, Role.MENTOR}:
        raise forbidden("password reset not allowed")
    updated = update_user_record(target, password_hash=hash_password(payload.password))
    record_audit(user.id, "user.password.reset", "user", str(updated.id))
    return to_user_info(updated)


def delete_user(user: UserRecord, user_id: int) -> UserInfo:
    """删除平台用户并同步删除其 Linux 子账户，最后一个管理员受到保护。"""
    require_permission(user.role, "users:delete")
    target = next((record for record in DEMO_USERS if record.id == user_id), None)
    if target is None:
        raise not_found("user not found")
    if target.role == Role.ADMIN and count_admin_users() <= 1:
        raise forbidden("last admin user cannot be deleted")
    info = to_user_info(target)
    account_plan = delete_child_account(target.username, target.role.value)
    remove_user_record(user_id)
    record_audit(
        user.id,
        "user.delete",
        "user",
        str(target.id),
        detail_json={
            "role": target.role.value,
            "linux_account_name": account_plan.account_name,
            "linux_account_executed": account_plan.executed,
        },
    )
    return info


def count_admin_users() -> int:
    """统计管理员数量，用于防止删除最后一个可管理系统的账号。"""
    return sum(1 for record in DEMO_USERS if record.role == Role.ADMIN)


def to_user_info(record: UserRecord) -> UserInfo:
    """把内部用户记录转换为用户管理接口的安全响应模型。"""
    settings = get_settings()
    role = record.role.value
    return UserInfo(
        id=record.id,
        username=record.username,
        real_name=record.real_name,
        role=role,
        state=record.state,
        home_path=home_path_for_user(record.username, role, settings),
        linux_account_name=linux_account_for_role(record.username, role, settings),
        created_at=utc_now(),
    )
