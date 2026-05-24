import logging

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, forbidden, not_found, validation_error
from app.core.rbac import Role, require_permission
from app.core.security import hash_password
from app.core.time_utils import local_datetime
from app.db.models import User, UserSupervisor
from app.db.session import SessionLocal
from app.schemas.users import (
    UserCreateRequest,
    UserInfo,
    UserListRequest,
    UserPasswordResetRequest,
    UserUpdateRequest,
)
from app.services.audit_service import record_audit, utc_now
from app.services.auth_service import UserRecord, replace_supervisors, revoke_user_sessions, user_model_to_record
from app.services.linux_account_service import (
    create_child_account,
    delete_child_account,
    ensure_child_account,
    home_path_for_user,
    linux_account_for_role,
    set_child_account_password,
)
from app.services.samba_service import delete_samba_account, disable_samba_account, samba_status_label, set_samba_password

logger = logging.getLogger(__name__)


def list_users(user: UserRecord, payload: UserListRequest | None = None) -> list[UserInfo]:
    """返回数据库中的用户列表，导师只能看到自己名下的学生。"""
    require_permission(user.role, "users:read")
    payload = payload or UserListRequest()
    with SessionLocal() as db:
        statement = select(User).order_by(User.id)
        if user.role == Role.MENTOR:
            statement = (
                statement.join(UserSupervisor, UserSupervisor.student_id == User.id)
                .where(UserSupervisor.supervisor_id == user.id)
                .where(User.role == Role.STUDENT.value)
            )
        if payload.user_id is not None:
            statement = statement.where(User.id == payload.user_id)
        if payload.role:
            statement = statement.where(User.role == payload.role)
        if payload.state:
            statement = statement.where(User.state == payload.state)
        if payload.keyword:
            keyword = payload.keyword.strip().lower()
            statement = statement.where(
                or_(
                    func.lower(User.username).contains(keyword),
                    func.lower(User.real_name).contains(keyword),
                    User.id == int(keyword) if keyword.isdigit() else False,
                )
            )
        records = [user_model_to_record(model, db) for model in db.scalars(statement).all()]
        return [to_user_info(record, db) for record in records]


def create_user(user: UserRecord, payload: UserCreateRequest) -> UserInfo:
    """创建平台用户，并持久化用户基础信息、Linux 账户映射和导师关系。"""
    target_role = Role(payload.role)
    if user.role == Role.MENTOR and target_role != Role.STUDENT:
        raise forbidden("mentor can only create student users")
    if target_role == Role.STUDENT:
        require_permission(user.role, "users:create_student")
    else:
        require_permission(user.role, "users:create")

    with SessionLocal() as db:
        if username_exists(db, payload.username):
            raise validation_error("username already exists")
        if payload.user_id is not None and db.get(User, payload.user_id) is not None:
            raise validation_error("user id already exists")
        supervisor_ids = resolve_supervisor_ids(actor=user, target_role=target_role, requested_ids=payload.supervisor_ids, db=db)
        role_value = target_role.value
        account_name = linux_account_for_role(payload.username, role_value)
        home_path = home_path_for_user(payload.username, role_value)
        model = User(
            username=payload.username,
            real_name=payload.real_name,
            role=role_value,
            state=payload.state,
            password_hash=hash_password(payload.password),
            home_path=home_path,
            linux_account_name=account_name,
            samba_enabled=False,
            samba_status="disabled",
        )
        if payload.user_id is not None:
            model.id = payload.user_id
        db.add(model)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise validation_error("username or user id already exists") from exc

        account_plan = None
        if target_role in {Role.STUDENT, Role.MENTOR, Role.ADMIN}:
            account_plan = create_child_account(model.username, model.id, model.role, password=payload.password)
            model.home_path = account_plan.home_path
            model.linux_account_name = account_plan.account_name
        for supervisor_id in supervisor_ids:
            db.add(UserSupervisor(student_id=model.id, supervisor_id=supervisor_id))
        db.commit()
        db.refresh(model)
        record = user_model_to_record(model, db)

    record_audit(
        user.id,
        "user.create",
        "user",
        str(record.id),
        detail_json={
            "username": record.username,
            "role": record.role.value,
            "linux_account_name": record.linux_account_name,
            "linux_account_executed": account_plan.executed if account_plan is not None else False,
            "home_path": record.home_path,
            "supervisor_ids": list(record.supervisor_ids),
        },
    )
    return to_user_info(record)


def update_user(user: UserRecord, payload: UserUpdateRequest) -> UserInfo:
    """更新用户资料、角色、状态和导师关系，并同步刷新持久化账户映射。"""
    require_permission(user.role, "users:read")
    with SessionLocal() as db:
        target_model = find_target_user_model(db, payload.user_id)
        if target_model is None:
            raise not_found("user not found")
        target = user_model_to_record(target_model, db)
        if user.role == Role.MENTOR and (target.role != Role.STUDENT or user.id not in target.supervisor_ids):
            raise forbidden("mentor can only manage assigned student users")
        if user.role != Role.ADMIN and user.id != target.id and target.role != Role.STUDENT:
            raise forbidden("user update not allowed")

        data = payload.model_dump(exclude_unset=True)
        data.pop("user_id", None)
        if "username" in data and data["username"] is not None:
            if user.role != Role.ADMIN:
                raise forbidden("only admin can change usernames")
            if username_exists(db, data["username"], exclude_user_id=target.id):
                raise validation_error("username already exists")
        if "role" in data and data["role"] is not None:
            next_role = Role(data["role"])
            if user.role != Role.ADMIN:
                raise forbidden("only admin can change roles")
            if target.role == Role.ADMIN and next_role != Role.ADMIN and count_admin_users(db) <= 1:
                raise forbidden("last admin user cannot be downgraded")
            data["role"] = next_role.value
        state_changed_to_disabled = data.get("state") == "disabled" and target.state != "disabled"
        if "state" in data and data["state"] is not None and target.role == Role.ADMIN and data["state"] != "enabled" and count_admin_users(db) <= 1:
            raise forbidden("last admin user cannot be disabled")

        next_role_value = data.get("role", target.role.value)
        next_role = Role(next_role_value)
        supervisor_ids_changed = False
        if "supervisor_ids" in data:
            if user.role != Role.ADMIN:
                data.pop("supervisor_ids", None)
            elif data.get("supervisor_ids") is not None:
                supervisor_ids = tuple(resolve_supervisor_ids(actor=user, target_role=next_role, requested_ids=data["supervisor_ids"], db=db))
                supervisor_ids_changed = True
            else:
                supervisor_ids = ()
                supervisor_ids_changed = True
        elif "role" in data and next_role != Role.STUDENT:
            supervisor_ids = ()
            supervisor_ids_changed = True
        else:
            supervisor_ids = target.supervisor_ids

        if not data and not supervisor_ids_changed:
            return to_user_info(target, db)

        if "samba_enabled" in data and data["samba_enabled"] is not None:
            next_samba_enabled = bool(data["samba_enabled"])
            if next_samba_enabled and not target.samba_enabled:
                raise validation_error("samba enable requires current password")
            if not next_samba_enabled and target.samba_enabled and target_model.linux_account_name:
                samba_plan = disable_samba_account(target_model.linux_account_name)
                data["samba_status"] = samba_plan.status
                data["samba_last_error"] = samba_plan.message if samba_plan.status == "failed" else None
        for field in ("username", "real_name", "role", "state", "password_hash", "samba_enabled", "samba_status", "samba_last_error"):
            if field in data and data[field] is not None:
                setattr(target_model, field, data[field])
        if any(key.startswith("samba_") for key in data):
            target_model.samba_updated_at = local_datetime()
        if "username" in data or "role" in data:
            target_model.home_path = home_path_for_user(target_model.username, target_model.role)
            target_model.linux_account_name = linux_account_for_role(target_model.username, target_model.role)
        if supervisor_ids_changed:
            replace_supervisors(db, target_model.id, tuple(supervisor_ids))
        if target.role == Role.MENTOR and next_role != Role.MENTOR:
            db.query(UserSupervisor).filter(UserSupervisor.supervisor_id == target.id).delete(synchronize_session=False)
        db.commit()
        db.refresh(target_model)
        updated = user_model_to_record(target_model, db)

    audit_detail = {key: str(value) for key, value in data.items() if key != "password_hash"}
    if supervisor_ids_changed:
        audit_detail["supervisor_ids"] = ",".join(str(item) for item in updated.supervisor_ids)
    if state_changed_to_disabled:
        audit_detail["revoked_sessions"] = str(revoke_user_sessions(updated.id))
    record_audit(user.id, "user.update", "user", str(updated.id), detail_json=audit_detail)
    return to_user_info(updated)


def reset_user_password(user: UserRecord, payload: UserPasswordResetRequest) -> UserInfo:
    """管理员重置任意账号密码；导师可重置自己学生账号密码。"""
    require_permission(user.role, "users:read")
    with SessionLocal() as db:
        target_model = find_target_user_model(db, payload.user_id)
        if target_model is None:
            raise not_found("user not found")
        target = user_model_to_record(target_model, db)
        if user.role == Role.MENTOR and (target.role != Role.STUDENT or user.id not in target.supervisor_ids):
            raise forbidden("mentor can only reset assigned student passwords")
        if user.role not in {Role.ADMIN, Role.MENTOR}:
            raise forbidden("password reset not allowed")
        target_model.password_hash = hash_password(payload.password)
        db.commit()
        db.refresh(target_model)
        updated = user_model_to_record(target_model, db)
    account_plan = set_child_account_password(updated.username, updated.role.value, payload.password)
    samba_plan = None
    if updated.samba_enabled and updated.linux_account_name:
        try:
            samba_plan = set_samba_password(updated.linux_account_name, payload.password, enabled=True)
        except AppError as exc:
            update_user_record_samba_status(updated.id, "failed", exc.message)
            raise
        updated = update_user_record_samba_status(
            updated.id,
            samba_plan.status,
            samba_plan.message if samba_plan.status == "failed" else None,
        )
    revoked_sessions = revoke_user_sessions(updated.id)
    record_audit(
        user.id,
        "user.password.reset",
        "user",
        str(updated.id),
        detail_json={
            "revoked_sessions": revoked_sessions,
            "linux_account_name": account_plan.account_name,
            "linux_account_executed": account_plan.executed,
            "samba_status": samba_plan.status if samba_plan is not None else updated.samba_status,
            "samba_executed": samba_plan.executed if samba_plan is not None else False,
        },
    )
    return to_user_info(updated)


def delete_user(user: UserRecord, user_id: int) -> UserInfo:
    """删除平台用户并同步处理 Linux 子账户；最后一个管理员受到保护。"""
    require_permission(user.role, "users:delete")
    with SessionLocal() as db:
        target_model = find_target_user_model(db, user_id)
        if target_model is None:
            raise not_found("user not found")
        target = user_model_to_record(target_model, db)
        if target.role == Role.ADMIN and count_admin_users(db) <= 1:
            raise forbidden("last admin user cannot be deleted")
        info = to_user_info(target, db)
        account_plan = None
        samba_plan = None
        if target.role in {Role.ADMIN, Role.STUDENT, Role.MENTOR}:
            if target.linux_account_name and (target.samba_enabled or target.samba_status not in {"", "disabled"}):
                samba_plan = delete_samba_account(target.linux_account_name)
            account_plan = delete_child_account(target.username, target.role.value)
        db.query(UserSupervisor).filter(
            or_(UserSupervisor.student_id == target.id, UserSupervisor.supervisor_id == target.id)
        ).delete(synchronize_session=False)
        db.delete(target_model)
        db.commit()

    revoke_user_sessions(target.id)
    record_audit(
        user.id,
        "user.delete",
        "user",
        str(target.id),
        detail_json={
            "username": target.username,
            "role": target.role.value,
            "linux_account_name": account_plan.account_name if account_plan is not None else target.linux_account_name,
            "linux_account_executed": account_plan.executed if account_plan is not None else False,
            "samba_status": samba_plan.status if samba_plan is not None else target.samba_status,
            "samba_executed": samba_plan.executed if samba_plan is not None else False,
        },
    )
    return info


def resolve_supervisor_ids(
    actor: UserRecord,
    target_role: Role,
    requested_ids: list[int] | tuple[int, ...] | None,
    db: Session | None = None,
) -> list[int]:
    """校验并规范学生导师关系；每名学生最多绑定两名导师。"""
    if target_role != Role.STUDENT:
        return []
    if actor.role == Role.MENTOR:
        return [actor.id]
    owns_session = db is None
    active_db = db or SessionLocal()
    try:
        requested = [] if requested_ids is None else [int(item) for item in requested_ids if item is not None]
        deduped: list[int] = []
        for supervisor_id in requested:
            if supervisor_id not in deduped:
                deduped.append(supervisor_id)
        if len(deduped) > 2:
            raise validation_error("student can have at most two supervisors")
        for supervisor_id in deduped:
            mentor = find_target_user_model(active_db, supervisor_id)
            if mentor is None or mentor.role != Role.MENTOR.value:
                raise validation_error("supervisor must be mentor user")
        return deduped
    finally:
        if owns_session:
            active_db.close()


def find_target_user(user_id: int) -> UserRecord | None:
    """按统一识别码查找平台用户。"""
    with SessionLocal() as db:
        model = find_target_user_model(db, user_id)
        return user_model_to_record(model, db) if model is not None else None


def find_target_user_model(db: Session, user_id: int) -> User | None:
    """按主键查找用户 ORM 模型。"""
    return db.get(User, user_id)


def next_auto_user_id() -> int:
    """返回下一个可用用户 ID，主要供旧调用兼容使用。"""
    with SessionLocal() as db:
        max_id = db.scalar(select(func.max(User.id))) or 0
        return max_id + 1


def count_admin_users(db: Session | None = None) -> int:
    """统计管理员数量，用于防止删除最后一个可管理系统的账号。"""
    owns_session = db is None
    active_db = db or SessionLocal()
    try:
        return active_db.scalar(select(func.count()).select_from(User).where(User.role == Role.ADMIN.value)) or 0
    finally:
        if owns_session:
            active_db.close()


def ensure_existing_user_linux_accounts() -> int:
    """启动时为历史平台用户补齐 Linux 账户和文件根目录。"""
    created_or_existing = 0
    with SessionLocal() as db:
        for model in db.scalars(select(User).order_by(User.id)).all():
            username_for_log = model.username
            try:
                expected_home = home_path_for_user(model.username, model.role)
                if model.role in {Role.ADMIN.value, Role.STUDENT.value, Role.MENTOR.value} and model.home_path != expected_home:
                    model.home_path = expected_home
                elif not model.home_path:
                    model.home_path = expected_home
                if not model.linux_account_name:
                    model.linux_account_name = linux_account_for_role(model.username, model.role)
                if model.samba_enabled is None:
                    model.samba_enabled = False
                if not model.samba_status:
                    model.samba_status = "disabled"
                if model.role in {Role.ADMIN.value, Role.STUDENT.value, Role.MENTOR.value}:
                    account_plan = ensure_child_account(model.username, model.role)
                    model.home_path = account_plan.home_path
                    model.linux_account_name = account_plan.account_name
                    created_or_existing += 1
                db.commit()
            except Exception:
                # 历史用户可能存在不符合 Linux 用户名规则的账号，或部署机 sudoers 尚未放开 useradd/chpasswd。
                # 启动补齐失败需要进入日志供管理员修复，但不能让一个异常账户把整套 Web API 拖成 502。
                db.rollback()
                logger.exception("failed to ensure Linux account for existing user %s", username_for_log)
    return created_or_existing


def ensure_existing_user_home_directories() -> int:
    """兼容旧启动调用：实际会同时补齐 Linux 账户和用户根目录。"""
    return ensure_existing_user_linux_accounts()


def username_exists(db: Session, username: str, exclude_user_id: int | None = None) -> bool:
    """检查用户名是否已存在，编辑时可排除当前用户。"""
    statement = select(User.id).where(func.lower(User.username) == username.strip().lower())
    if exclude_user_id is not None:
        statement = statement.where(User.id != exclude_user_id)
    return db.scalar(statement) is not None


def to_user_info(record: UserRecord, db: Session | None = None) -> UserInfo:
    """把内部用户记录转换为用户管理接口的安全响应模型。"""
    owns_session = db is None
    active_db = db or SessionLocal()
    try:
        supervisor_names = [
            name
            for name in active_db.scalars(
                select(User.real_name)
                .where(User.id.in_(record.supervisor_ids))
                .order_by(User.id)
            ).all()
        ] if record.supervisor_ids else []
        return UserInfo(
            id=record.id,
            username=record.username,
            real_name=record.real_name,
            role=record.role.value,
            state=record.state,
            home_path=record.home_path or home_path_for_user(record.username, record.role.value),
            linux_account_name=record.linux_account_name or linux_account_for_role(record.username, record.role.value),
            linux_uid=record.linux_uid,
            linux_gid=record.linux_gid,
            samba_enabled=record.samba_enabled,
            samba_status=record.samba_status or "disabled",
            samba_status_label=samba_status_label(record.samba_status or "disabled"),
            samba_last_error=record.samba_last_error,
            supervisor_ids=list(record.supervisor_ids),
            supervisor_names=supervisor_names,
            created_at=record.created_at or utc_now(),
        )
    finally:
        if owns_session:
            active_db.close()


def update_user_record_samba_status(user_id: int, status: str, last_error: str | None) -> UserRecord:
    """单独更新 Samba 状态，避免密码重置流程重新拼装整份用户资料。"""
    with SessionLocal() as db:
        model = db.get(User, user_id)
        if model is None:
            raise not_found("user not found")
        model.samba_status = status
        model.samba_last_error = last_error
        model.samba_updated_at = local_datetime()
        db.commit()
        db.refresh(model)
        return user_model_to_record(model, db)
