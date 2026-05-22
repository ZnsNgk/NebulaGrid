from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.models import Setting, User
from app.db.session import engine
from app.services.audit_service import default_settings


def create_schema() -> None:
    """创建当前 ORM 定义的全部缺失数据表，适合 MVP 首次部署初始化。"""
    Base.metadata.create_all(bind=engine)


def migrate_existing_schema() -> None:
    """补齐已存在数据库中的用户管理列。

    SQLAlchemy 的 create_all 只会创建缺失的数据表，不会修改已有表结构。用户管理
    从内存数据切换到 PostgreSQL 后，旧库里可能已经存在 users/login_sessions 表，
    但缺少新代码读取或写入的列；此时登录会因为 UndefinedColumn 变成 500。
    这里仅执行幂等的 ADD COLUMN/CREATE INDEX，不删除、不改名、不覆盖已有数据。
    """
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        if "users" in tables:
            ensure_columns(
                connection,
                "users",
                {
                    "state": "VARCHAR(32) DEFAULT 'enabled'",
                    "home_path": "VARCHAR(1024)",
                    "linux_account_name": "VARCHAR(64)",
                    "linux_uid": "INTEGER",
                    "linux_gid": "INTEGER",
                    "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT now()",
                },
            )
        if "login_sessions" in tables:
            ensure_columns(
                connection,
                "login_sessions",
                {
                    "token_hash": "VARCHAR(128)",
                    "user_agent": "VARCHAR(512)",
                    "ip": "VARCHAR(64)",
                    "login_device": "VARCHAR(128)",
                    "device_id": "VARCHAR(128)",
                    "last_seen_at": "TIMESTAMP WITH TIME ZONE",
                    "logout_at": "TIMESTAMP WITH TIME ZONE",
                    "expires_at": "TIMESTAMP WITH TIME ZONE",
                    "revoked_at": "TIMESTAMP WITH TIME ZONE",
                    "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT now()",
                },
            )
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_login_sessions_token_hash ON login_sessions (token_hash)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_login_sessions_device_id ON login_sessions (device_id)"))
        if "user_supervisors" in tables:
            ensure_columns(
                connection,
                "user_supervisors",
                {
                    "id": "SERIAL",
                    "student_id": "INTEGER",
                    "supervisor_id": "INTEGER",
                },
            )
        if "audit_logs" in tables:
            ensure_columns(
                connection,
                "audit_logs",
                {
                    "actor_user_id": "INTEGER",
                    "action": "VARCHAR(128)",
                    "target_type": "VARCHAR(64)",
                    "target_id": "VARCHAR(128)",
                    "ip": "VARCHAR(64)",
                    "result": "VARCHAR(32) DEFAULT 'success'",
                    "detail_json": "JSON DEFAULT '{}'::json",
                    "created_at": "TIMESTAMP WITH TIME ZONE DEFAULT now()",
                },
            )
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_logs_action ON audit_logs (action)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_logs_target_type ON audit_logs (target_type)"))
        if "nodes" in tables:
            ensure_columns(
                connection,
                "nodes",
                {
                    # 节点可见性从单 owner 过渡到多 owner；旧字段继续保留，避免历史逻辑失效。
                    "owner_user_ids": "JSON DEFAULT '[]'::json",
                    "access_scope": "VARCHAR(32) DEFAULT 'public'",
                    "sharing_scope": "VARCHAR(32) DEFAULT 'public'",
                },
            )
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_nodes_access_scope ON nodes (access_scope)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_nodes_sharing_scope ON nodes (sharing_scope)"))
        if "gpus" in tables:
            ensure_columns(
                connection,
                "gpus",
                {
                    # Runtime Guard 以 GPU UUID 作为长期稳定标识，避免节点重启后 index 漂移造成误判。
                    "gpu_uuid": "VARCHAR(128) DEFAULT ''",
                },
            )
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_gpus_gpu_uuid ON gpus (gpu_uuid)"))
        if "tasks" in tables:
            ensure_columns(
                connection,
                "tasks",
                {
                    # 任务调度从原型内存列表切换到数据库后，需要把执行命令、阻塞原因、
                    # 日志路径和返回码都留在主表，避免 worker 重启后丢失运行上下文。
                    "generated_command": "TEXT DEFAULT ''",
                    "urgent": "BOOLEAN DEFAULT false",
                    "last_block_reason": "VARCHAR(512) DEFAULT ''",
                    "log_path": "VARCHAR(1024) DEFAULT ''",
                    "return_code": "INTEGER",
                },
            )
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_urgent ON tasks (urgent)"))
        if "env_install_jobs" in tables:
            ensure_columns(
                connection,
                "env_install_jobs",
                {
                    # worker 需要从数据库恢复待执行安装命令，避免 API 请求同步阻塞和进程重启丢任务。
                    "command": "TEXT DEFAULT ''",
                    "workdir": "VARCHAR(1024) DEFAULT ''",
                    # 编译安装需要区分主节点、本地默认 GPU、CPU 隐藏 GPU 和指定 GPU，避免重启后丢失执行约束。
                    "compile_on_master": "BOOLEAN DEFAULT false",
                    "gpu_visibility": "VARCHAR(32) DEFAULT 'default'",
                },
            )


def ensure_columns(connection, table_name: str, columns: dict[str, str]) -> None:
    """按当前数据库实际列集合补齐缺失列，重复运行不会报错。"""
    existing = {
        row[0]
        for row in connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table_name"
            ),
            {"table_name": table_name},
        )
    }
    for column_name, column_type in columns.items():
        if column_name in existing:
            continue
        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))


def seed_defaults(db: Session) -> None:
    """写入默认管理员和基础配置，重复运行时不会覆盖已有业务数据。"""
    settings = get_settings()
    admin = db.scalar(select(User).where(User.username == "admin"))
    if admin is None:
        db.add(
            User(
                username="admin",
                real_name="System Admin",
                role="admin",
                password_hash=hash_password("admin123"),
                state="enabled",
                home_path=f"/home/{settings.main_linux_user}",
                linux_account_name=settings.main_linux_user,
            )
        )
    else:
        # 旧库升级时 admin 可能已存在，但新增的映射字段为空；登录后的用户管理需要这些值可展示。
        if not admin.state:
            admin.state = "enabled"
        if not admin.home_path:
            admin.home_path = f"/home/{settings.main_linux_user}"
        if not admin.linux_account_name:
            admin.linux_account_name = settings.main_linux_user
    for key, value in default_settings().items():
        exists = db.get(Setting, key)
        if exists is None:
            db.add(Setting(key=key, value=value))
    db.commit()


def init_database() -> None:
    """初始化数据库结构和默认数据，供应用启动和命令行脚本直接调用。"""
    create_schema()
    migrate_existing_schema()
    with Session(engine) as db:
        seed_defaults(db)
