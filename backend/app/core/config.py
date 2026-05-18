import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    """保存后端运行所需的非敏感配置，敏感项后续从 secrets/env 注入。"""

    app_name: str = "NebulaGrid"
    app_version: str = "0.1.0"
    environment: str = "development"
    data_root: str = "/home/ddltm/data"
    user_home_root: str = "/home/ddltm/data/user"
    user_workspace_alias: str = "/workspace"
    visible_roots: tuple[str, ...] = (
        "/home/ddltm/data/user",
        "/home/ddltm/envs/user_envs",
        "/home/ddltm/envs/miniconda3",
    )
    database_url: str = "postgresql+psycopg://nebulagrid:nebulagrid@127.0.0.1:5432/nebulagrid"
    redis_url: str = "redis://127.0.0.1:6379/0"
    influxdb_url: str = "http://127.0.0.1:8086"
    influxdb_org: str = "nebulagrid"
    influxdb_bucket: str = "nebulagrid_metrics"
    influxdb_token: str = ""
    influxdb_latest_range: str = "30m"
    task_log_root: str = "/home/ddltm/data/logs/task_logs"
    env_package_root: str = "/home/ddltm/envs/packages"
    env_install_log_root: str = "/home/ddltm/data/logs/env_install_logs"
    runtime_root: str = "/home/ddltm/data/runtime"
    remote_code_root: str = "/home/ddltm/envs/nebulagrid_remote"
    miniconda_python: str = "/home/ddltm/envs/miniconda3/bin/python"
    main_linux_user: str = "ddltm"
    session_secret: str = "change-this-session-secret"
    scheduler_interval_seconds: int = 5
    monitor_interval_seconds: int = 5
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "null",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """从环境变量构造配置对象，并缓存结果避免每次请求重复解析。"""
    visible_roots = os.getenv(
        "NEBULAGRID_VISIBLE_ROOTS",
        "/home/ddltm/data/user,/home/ddltm/envs/user_envs,/home/ddltm/envs/miniconda3",
    )
    return Settings(
        app_name=os.getenv("NEBULAGRID_APP_NAME", "NebulaGrid"),
        app_version=os.getenv("NEBULAGRID_APP_VERSION", "0.1.0"),
        environment=os.getenv("NEBULAGRID_ENV", "development"),
        data_root=os.getenv("NEBULAGRID_DATA_ROOT", "/home/ddltm/data"),
        user_home_root=os.getenv("NEBULAGRID_USER_HOME_ROOT", "/home/ddltm/data/user"),
        user_workspace_alias=os.getenv("NEBULAGRID_WORKSPACE_ALIAS", "/workspace"),
        visible_roots=tuple(root.strip() for root in visible_roots.split(",") if root.strip()),
        database_url=os.getenv(
            "NEBULAGRID_DATABASE_URL",
            "postgresql+psycopg://nebulagrid:nebulagrid@127.0.0.1:5432/nebulagrid",
        ),
        redis_url=os.getenv("NEBULAGRID_REDIS_URL", "redis://127.0.0.1:6379/0"),
        influxdb_url=os.getenv("NEBULAGRID_INFLUXDB_URL", "http://127.0.0.1:8086"),
        influxdb_org=os.getenv("NEBULAGRID_INFLUXDB_ORG", "nebulagrid"),
        influxdb_bucket=os.getenv("NEBULAGRID_INFLUXDB_BUCKET", "nebulagrid_metrics"),
        influxdb_token=os.getenv("NEBULAGRID_INFLUXDB_TOKEN", ""),
        influxdb_latest_range=os.getenv("NEBULAGRID_INFLUXDB_LATEST_RANGE", "30m"),
        task_log_root=os.getenv("NEBULAGRID_TASK_LOG_ROOT", "/home/ddltm/data/logs/task_logs"),
        env_package_root=os.getenv("NEBULAGRID_ENV_PACKAGE_ROOT", "/home/ddltm/envs/packages"),
        env_install_log_root=os.getenv("NEBULAGRID_ENV_INSTALL_LOG_ROOT", "/home/ddltm/data/logs/env_install_logs"),
        runtime_root=os.getenv("NEBULAGRID_RUNTIME_ROOT", "/home/ddltm/data/runtime"),
        remote_code_root=os.getenv("NEBULAGRID_REMOTE_CODE_ROOT", "/home/ddltm/envs/nebulagrid_remote"),
        miniconda_python=os.getenv("NEBULAGRID_MINICONDA_PYTHON", "/home/ddltm/envs/miniconda3/bin/python"),
        main_linux_user=os.getenv("NEBULAGRID_MAIN_LINUX_USER", "ddltm"),
        session_secret=os.getenv("NEBULAGRID_SESSION_SECRET", "change-this-session-secret"),
        scheduler_interval_seconds=int(os.getenv("NEBULAGRID_SCHEDULER_INTERVAL_SECONDS", "5")),
        monitor_interval_seconds=int(os.getenv("NEBULAGRID_MONITOR_INTERVAL_SECONDS", "5")),
        cors_origins=tuple(
            origin.strip()
            for origin in os.getenv(
                "NEBULAGRID_CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8080,http://localhost:8080,null",
            ).split(",")
            if origin.strip()
        ),
    )
