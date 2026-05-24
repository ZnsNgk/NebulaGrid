from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ACCOUNT_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
INFLUX_START = "1970-01-01T00:00:00Z"
DEFAULT_INFLUX_PREDICATES = ('_measurement="node_metrics"', '_measurement="gpu_metrics"')
INFLUX_PREDICATE = ""


@dataclass(frozen=True)
class AccountCandidate:
    """记录需要清理的测试系统账户，避免数据库清空后丢失 Linux/Samba 账户名。"""

    username: str
    role: str
    linux_account_name: str
    samba_enabled: bool
    samba_status: str


@dataclass(frozen=True)
class AccountCleanupMode:
    """拆分 Linux 与 Samba 的执行开关，避免只开启一种管理能力时误动另一类账户。"""

    linux: bool
    samba: bool


def main() -> None:
    """上线前清空测试数据，并恢复 PostgreSQL 与 InfluxDB 到初始状态。"""
    args = parse_args()
    from app.core.config import get_settings

    settings = get_settings()

    if args.yes:
        from app.db.init_db import create_schema, migrate_existing_schema

        create_schema()
        migrate_existing_schema()
    account_candidates = collect_account_candidates(settings.main_linux_user)
    table_names = list_model_table_names()

    print_plan(args, table_names, account_candidates)
    if not args.yes:
        print("\n当前为预览模式，未修改数据库或系统账户。确认无误后加 --yes 执行。")
        return

    cleanup_mode = resolve_account_cleanup_mode(args)
    if account_candidates and (cleanup_mode.linux or cleanup_mode.samba):
        cleanup_system_accounts(account_candidates, cleanup_mode)
    elif account_candidates:
        print("\n跳过 Linux/Samba 系统账户删除：未启用管理配置，也未传入 --force-system-accounts。")

    clear_postgres(table_names)
    clear_influxdb(args)
    print("\n清理完成：PostgreSQL 已恢复默认 admin/settings，InfluxDB 指标已清空。")


def parse_args() -> argparse.Namespace:
    """解析命令行参数；破坏性动作必须显式确认，降低误操作风险。"""
    parser = argparse.ArgumentParser(
        description="清空 NebulaGrid PostgreSQL 与 InfluxDB 测试数据，并清理测试 Linux/Samba 账户。"
    )
    parser.add_argument("--yes", action="store_true", help="真正执行清理；不加该参数只打印计划。")
    parser.add_argument(
        "--skip-influxdb",
        action="store_true",
        help="跳过 InfluxDB 指标删除；用于 token 未配置或只想重置业务库的场景。",
    )
    parser.add_argument(
        "--skip-system-accounts",
        action="store_true",
        help="跳过 Linux/Samba 账户删除；只清空数据库时使用。",
    )
    parser.add_argument(
        "--force-system-accounts",
        action="store_true",
        help="即使配置未开启账户管理，也实际执行 userdel/smbpasswd 清理测试账户。",
    )
    parser.add_argument(
        "--influx-predicate",
        default=INFLUX_PREDICATE,
        help="InfluxDB delete predicate；默认只删除 node_metrics 和 gpu_metrics。",
    )
    return parser.parse_args()


def collect_account_candidates(main_linux_user: str) -> list[AccountCandidate]:
    """在清空用户表前收集非初始管理员的 Linux/Samba 账户，后续按名单逐个清理。"""
    from sqlalchemy import select

    from app.db.models import User
    from app.db.session import engine

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                User.username,
                User.role,
                User.linux_account_name,
                User.samba_enabled,
                User.samba_status,
            ).order_by(User.id)
        ).all()

    candidates: list[AccountCandidate] = []
    for username, role, linux_account_name, samba_enabled, samba_status in rows:
        if username == "admin":
            continue
        account_name = linux_account_name or username
        if not account_name or account_name == main_linux_user:
            continue
        if not ACCOUNT_NAME_PATTERN.fullmatch(account_name):
            print(f"跳过非法系统账户名 {account_name!r}，请人工确认。")
            continue
        candidates.append(
            AccountCandidate(
                username=username,
                role=role,
                linux_account_name=account_name,
                samba_enabled=bool(samba_enabled),
                samba_status=samba_status or "disabled",
            )
        )
    return candidates


def list_model_table_names() -> list[str]:
    """只清理当前 ORM 管理的业务表，避免误动同库中其他系统或运维表。"""
    from app.db.base import Base

    return [table.name for table in reversed(Base.metadata.sorted_tables)]


def print_plan(args: argparse.Namespace, table_names: list[str], account_candidates: list[AccountCandidate]) -> None:
    """输出清理计划，执行前让操作者明确会影响哪些数据库表和系统账户。"""
    print("NebulaGrid 上线前清理计划")
    print(f"- PostgreSQL 表数量：{len(table_names)}")
    print(f"- 将重置表：{', '.join(table_names)}")
    print(f"- 将保留并重建：admin / admin123 以及默认 settings")
    if args.skip_influxdb:
        print("- InfluxDB：跳过")
    else:
        print(f"- InfluxDB：删除 predicate = {', '.join(influx_delete_predicates(args))}")
    if args.skip_system_accounts:
        print("- Linux/Samba 账户：跳过")
    elif account_candidates:
        names = ", ".join(candidate.linux_account_name for candidate in account_candidates)
        print(f"- 待清理测试账户：{names}")
    else:
        print("- 待清理测试账户：无")


def resolve_account_cleanup_mode(args: argparse.Namespace) -> AccountCleanupMode:
    """结合配置和命令行判断 Linux 与 Samba 账户是否分别执行真实删除。"""
    from app.core.config import get_settings

    if args.skip_system_accounts:
        return AccountCleanupMode(linux=False, samba=False)
    if args.force_system_accounts:
        return AccountCleanupMode(linux=True, samba=True)
    values = current_setting_values()
    settings = get_settings()
    linux_enabled = setting_bool(values.get("manage.linux_accounts"), settings.manage_linux_accounts)
    samba_enabled = setting_bool(values.get("manage.samba_accounts"), settings.manage_samba_accounts)
    return AccountCleanupMode(linux=linux_enabled, samba=samba_enabled)


def current_setting_values() -> dict[str, str]:
    """读取当前 settings 表，账户清理必须发生在清空 settings 之前。"""
    from sqlalchemy import select

    from app.db.models import Setting
    from app.db.session import engine

    with engine.connect() as connection:
        rows = connection.execute(select(Setting.key, Setting.value)).all()
    return {key: value for key, value in rows}


def setting_bool(value: str | None, fallback: bool) -> bool:
    """兼容 settings 表和环境变量中的布尔写法。"""
    if value is None:
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on", "开启"}


def cleanup_system_accounts(candidates: list[AccountCandidate], mode: AccountCleanupMode) -> None:
    """先删 Samba 再删 Linux 账户，防止 SMB 凭据残留后仍可访问用户 home。"""
    print("\n开始清理 Linux/Samba 测试账户")
    for candidate in candidates:
        if mode.samba:
            delete_samba_account(candidate.linux_account_name)
        if mode.linux:
            delete_linux_account(candidate.linux_account_name)


def delete_samba_account(account_name: str) -> None:
    """删除 Samba 账户；账户不存在时 smbpasswd 可能返回非零，此处记录后继续清理 Linux 账户。"""
    smbpasswd = command_path("smbpasswd")
    systemctl = command_path("systemctl")
    if smbpasswd is None:
        print(f"- Samba {account_name}: 未找到 smbpasswd，跳过")
        return
    run_command([smbpasswd, "-x", account_name], allow_failure=True)
    if systemctl is not None:
        run_command([systemctl, "restart", "smbd"], allow_failure=True)


def delete_linux_account(account_name: str) -> None:
    """删除 Linux 子账户和 home；主账户和非法用户名已在收集阶段排除。"""
    userdel = command_path("userdel")
    if userdel is None:
        print(f"- Linux {account_name}: 未找到 userdel，跳过")
        return
    run_command([userdel, "--remove", account_name], allow_failure=True)


def command_path(name: str) -> str | None:
    """优先使用系统 PATH，缺失时查找常见绝对路径，便于 sudoers 精确授权。"""
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/usr/bin", "/usr/sbin", "/bin", "/sbin"):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    return None


def run_command(command: list[str], allow_failure: bool = False) -> None:
    """以非交互方式执行系统账户命令；非 root 运行时通过 sudo -n 触发受控授权。"""
    final_command = privileged_command(command)
    try:
        completed = subprocess.run(final_command, text=True, check=False, capture_output=True)
    except OSError as exc:
        if allow_failure:
            print(f"- {command[-1]}: 命令不可用，已跳过：{exc}")
            return
        raise
    if completed.returncode == 0:
        print(f"- {command[-1]}: 已执行 {' '.join(command[:-1])}")
        return
    message = (completed.stderr or completed.stdout or "").strip()
    if allow_failure:
        print(f"- {command[-1]}: 执行失败但继续：{message or completed.returncode}")
        return
    raise RuntimeError(message or f"command failed: {command}")


def privileged_command(command: list[str]) -> list[str]:
    """非 root 用户使用 sudo -n，避免上线脚本卡在交互式密码提示。"""
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() == 0:
        return command
    return ["sudo", "-n", *command]


def clear_postgres(table_names: list[str]) -> None:
    """TRUNCATE 当前 ORM 表并重置自增序列，再写入默认管理员和默认配置。"""
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.db.init_db import seed_defaults
    from app.db.session import engine
    from app.services.audit_service import default_settings

    if not table_names:
        return
    with engine.begin() as connection:
        preparer = connection.dialect.identifier_preparer
        quoted = ", ".join(preparer.quote(table_name) for table_name in table_names)
        connection.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
    with engine.begin() as connection:
        for key, value in default_settings().items():
            connection.execute(
                text("INSERT INTO settings (key, value) VALUES (:key, :value) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                {"key": key, "value": value},
            )
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM settings WHERE key LIKE 'influxdb.%' OR key IN ('database.url', 'redis.url', 'session.secret')"))
    with Session(engine) as db:
        seed_defaults(db)


def clear_influxdb(args: argparse.Namespace) -> None:
    """调用 InfluxDB 2.x delete API 清空 NebulaGrid 指标 bucket。"""
    from app.core.config import get_settings

    if args.skip_influxdb:
        return
    settings = get_settings()
    if not settings.influxdb_url or not settings.influxdb_org or not settings.influxdb_bucket or not settings.influxdb_token:
        print("\n跳过 InfluxDB：NEBULAGRID_INFLUXDB_URL/ORG/BUCKET/TOKEN 未完整配置。")
        return
    params = urllib.parse.urlencode({"org": settings.influxdb_org, "bucket": settings.influxdb_bucket})
    url = f"{settings.influxdb_url.rstrip('/')}/api/v2/delete?{params}"
    stop = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for predicate in influx_delete_predicates(args):
        delete_influx_predicate(url, settings.influxdb_token, stop, predicate)
    print("\nInfluxDB 指标已清空。")


def influx_delete_predicates(args: argparse.Namespace) -> list[str]:
    """默认分多次删除 measurement，避免 InfluxDB delete predicate 对 OR 支持不一致。"""
    custom_predicate = (args.influx_predicate or "").strip()
    if custom_predicate:
        return [custom_predicate]
    return list(DEFAULT_INFLUX_PREDICATES)


def delete_influx_predicate(url: str, token: str, stop: str, predicate: str) -> None:
    """执行单条 InfluxDB delete 请求，并在失败时透出响应体便于线上排查。"""
    body = {"start": INFLUX_START, "stop": stop, "predicate": predicate}
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status not in {204, 200}:
                raise RuntimeError(f"InfluxDB delete failed with status {response.status}, predicate={predicate!r}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"InfluxDB delete failed with HTTP {exc.code}, predicate={predicate!r}, response={detail}"
        ) from exc


if __name__ == "__main__":
    main()
