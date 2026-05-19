import argparse
from getpass import getpass

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import User
from app.db.session import SessionLocal


def main() -> None:
    """重置指定管理员账号密码，用于忘记初始密码或旧库迁移后无法登录。"""
    parser = argparse.ArgumentParser(description="Reset a NebulaGrid administrator password")
    parser.add_argument("--username", default="admin", help="administrator username, default: admin")
    parser.add_argument("--password", help="new password; omitted means prompt interactively")
    args = parser.parse_args()

    password = args.password or getpass("New password: ")
    if len(password) < 8:
        raise SystemExit("Password must be at least 8 characters")

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            raise SystemExit(f"User {args.username!r} does not exist")
        if user.role != "admin":
            raise SystemExit(f"User {args.username!r} is not an administrator")
        user.password_hash = hash_password(password)
        user.state = "enabled"
        db.commit()
    print(f"Password for administrator {args.username!r} has been reset")


if __name__ == "__main__":
    main()
