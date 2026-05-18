from app.db.init_db import init_database


def main() -> None:
    """命令行入口：创建数据库表并写入默认管理员与系统配置。"""
    init_database()
    print("NebulaGrid database initialized")


if __name__ == "__main__":
    main()
