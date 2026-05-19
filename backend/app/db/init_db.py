from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.models import Setting, User
from app.db.session import engine


def create_schema() -> None:
    """创建当前 ORM 定义的全部数据表，适合 MVP 首次部署初始化。"""
    Base.metadata.create_all(bind=engine)


def seed_defaults(db: Session) -> None:
    """写入默认管理员和基础配置，重复运行时不会覆盖已有数据。"""
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
    for key, value in {
        "scheduler.enabled": "true",
        "monitor.enabled": "true",
        "uploads.max_size_mb": "1024",
    }.items():
        exists = db.get(Setting, key)
        if exists is None:
            db.add(Setting(key=key, value=value))
    db.commit()


def init_database() -> None:
    """初始化数据库结构和默认数据，供命令行脚本直接调用。"""
    create_schema()
    with Session(engine) as db:
        seed_defaults(db)
