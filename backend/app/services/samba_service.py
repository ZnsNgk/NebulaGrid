import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings, get_settings
from app.core.errors import validation_error
from app.core.time_utils import local_now
from app.db.models import Setting
from app.db.session import SessionLocal

_SAMBA_ACCOUNT_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_KNOWN_COMMAND_PATHS = {
    "pdbedit": ("/usr/bin/pdbedit", "/usr/sbin/pdbedit", "/bin/pdbedit", "/sbin/pdbedit"),
    "smbpasswd": ("/usr/bin/smbpasswd", "/usr/sbin/smbpasswd", "/bin/smbpasswd", "/sbin/smbpasswd"),
}


@dataclass(frozen=True)
class SambaAccountPlan:
    """记录 Samba 账户同步结果，便于审计真实执行和本地干运行两种路径。"""

    account_name: str
    enabled: bool
    status: str
    status_label: str
    executed: bool
    commands: list[list[str]]
    message: str = ""
    updated_at: str = ""


def enable_samba_account(account_name: str, password: str) -> SambaAccountPlan:
    """创建或更新 Samba 账号并启用访问，密码必须来自刚校验过的系统登录密码。"""
    settings = get_settings()
    validate_samba_account_name(account_name)
    commands = [
        [command_path("smbpasswd"), "-s", "-a", account_name],
        [command_path("smbpasswd"), "-e", account_name],
    ]
    return run_samba_commands(account_name, True, commands, settings, password=password)


def disable_samba_account(account_name: str) -> SambaAccountPlan:
    """禁用 Samba 账号而不删除 Linux home，保留后续用户手动重新开启的可能。"""
    settings = get_settings()
    validate_samba_account_name(account_name)
    commands = [[command_path("smbpasswd"), "-d", account_name]]
    return run_samba_commands(account_name, False, commands, settings)


def delete_samba_account(account_name: str) -> SambaAccountPlan:
    """删除 Samba 账号，用户删除流程调用它避免遗留可访问凭据。"""
    settings = get_settings()
    validate_samba_account_name(account_name)
    commands = [[command_path("smbpasswd"), "-x", account_name]]
    return run_samba_commands(account_name, False, commands, settings)


def set_samba_password(account_name: str, password: str, enabled: bool) -> SambaAccountPlan:
    """同步 Samba 密码；仅在用户已开启 Samba 时调用，避免默认关闭用户被动创建 SMB 凭据。"""
    settings = get_settings()
    validate_samba_account_name(account_name)
    commands = [[command_path("smbpasswd"), "-s", account_name]]
    if enabled:
        commands.append([command_path("smbpasswd"), "-e", account_name])
    return run_samba_commands(account_name, enabled, commands, settings, password=password)


def inspect_samba_account(account_name: str, desired_enabled: bool, fallback_status: str = "disabled") -> SambaAccountPlan:
    """读取 Samba 后端中的实际账号状态；未启用真实管理时返回数据库最近状态。"""
    settings = get_settings()
    validate_samba_account_name(account_name)
    if not samba_management_enabled(settings) or os.name != "posix":
        status = fallback_status or ("enabled" if desired_enabled else "disabled")
        if desired_enabled and status == "disabled":
            status = "pending"
        return SambaAccountPlan(
            account_name=account_name,
            enabled=desired_enabled,
            status=status,
            status_label=samba_status_label(status),
            executed=False,
            commands=[],
            message="Samba 真实管理未启用，返回最近一次记录状态。",
            updated_at=local_now(),
        )
    command = [command_path("pdbedit"), "-v", "-u", account_name]
    try:
        completed = subprocess.run(privileged_command(command), text=True, check=False, capture_output=True)
    except OSError as exc:
        return samba_failure_plan(account_name, desired_enabled, [command], f"Samba 状态检查命令不可用：{exc}")
    if completed.returncode != 0:
        status = "failed" if desired_enabled else "disabled"
        message = (completed.stderr or completed.stdout or "").strip()
        return SambaAccountPlan(
            account_name=account_name,
            enabled=desired_enabled,
            status=status,
            status_label=samba_status_label(status),
            executed=True,
            commands=[command],
            message=message,
            updated_at=local_now(),
        )
    output = completed.stdout or ""
    status = "disabled" if account_flags_show_disabled(output) else "enabled"
    return SambaAccountPlan(
        account_name=account_name,
        enabled=status == "enabled",
        status=status,
        status_label=samba_status_label(status),
        executed=True,
        commands=[command],
        updated_at=local_now(),
    )


def samba_status_label(status: str) -> str:
    """把服务层状态转换成前端可直接展示的中文文案。"""
    return {
        "enabled": "已启用",
        "disabled": "已禁用",
        "failed": "失败",
        "pending": "未执行",
    }.get(status, "未知")


def validate_samba_account_name(account_name: str) -> None:
    """限制 Samba 用户名，防止把平台输入带入系统命令时越权操作其他账号。"""
    if not account_name or not _SAMBA_ACCOUNT_PATTERN.fullmatch(account_name):
        raise validation_error("samba account name must match ^[a-z_][a-z0-9_-]{0,31}$")


def command_path(name: str) -> str:
    """返回 Samba 命令路径；sudoers 推荐按绝对路径授权，缺失时保留原名用于报错。"""
    resolved = shutil.which(name)
    if resolved:
        return resolved
    for candidate in _KNOWN_COMMAND_PATHS.get(name, ()):
        if Path(candidate).exists():
            return candidate
    return name


def run_samba_commands(
    account_name: str,
    enabled: bool,
    commands: list[list[str]],
    settings: Settings,
    password: str | None = None,
) -> SambaAccountPlan:
    """按配置执行 Samba 命令；开发环境默认只返回计划，避免误改本机 Samba 数据库。"""
    status = "enabled" if enabled else "disabled"
    if not samba_management_enabled(settings) or os.name != "posix":
        return SambaAccountPlan(
            account_name=account_name,
            enabled=enabled,
            status="pending" if enabled else "disabled",
            status_label=samba_status_label("pending" if enabled else "disabled"),
            executed=False,
            commands=commands,
            message="Samba 真实管理未启用，命令未执行。",
            updated_at=local_now(),
        )
    for command in commands:
        try:
            subprocess.run(
                privileged_command(command),
                input=samba_password_input(command, password),
                text=True,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise validation_error(
                "samba account command failed",
                data={
                    "command": redacted_command(command),
                    "returncode": exc.returncode,
                    "stderr": (exc.stderr or "").strip(),
                },
            ) from exc
        except OSError as exc:
            raise validation_error("samba account command unavailable", data={"command": redacted_command(command), "error": str(exc)}) from exc
    return SambaAccountPlan(
        account_name=account_name,
        enabled=enabled,
        status=status,
        status_label=samba_status_label(status),
        executed=True,
        commands=commands,
        updated_at=local_now(),
    )


def samba_password_input(command: list[str], password: str | None) -> str | None:
    """为 smbpasswd 的非交互模式提供两遍密码输入，其他命令不写 stdin。"""
    if Path(command[0]).name != "smbpasswd" or password is None:
        return None
    if "-s" not in command:
        return None
    return f"{password}\n{password}\n"


def account_flags_show_disabled(output: str) -> bool:
    """解析 pdbedit 输出中的 Account Flags；包含 D 时表示 Samba 账号被禁用。"""
    for line in output.splitlines():
        if "Account Flags" not in line:
            continue
        return "D" in line
    return False


def samba_failure_plan(account_name: str, enabled: bool, commands: list[list[str]], message: str) -> SambaAccountPlan:
    """构造统一失败结果，调用方会持久化到用户记录供前端展示。"""
    return SambaAccountPlan(
        account_name=account_name,
        enabled=enabled,
        status="failed",
        status_label=samba_status_label("failed"),
        executed=False,
        commands=commands,
        message=message,
        updated_at=local_now(),
    )


def redacted_command(command: list[str]) -> list[str]:
    """审计或错误响应里不输出密码；当前命令参数不含明文密码，此函数保留统一出口。"""
    return list(command)


def samba_management_enabled(settings: Settings | None = None) -> bool:
    """读取落库的 Samba 管理总开关；数据库不可用时回退到环境变量默认值。"""
    resolved = settings or get_settings()
    try:
        with SessionLocal() as db:
            row = db.get(Setting, "manage.samba_accounts")
            if row is None:
                return bool(resolved.manage_samba_accounts)
            return row.value.strip().lower() in {"1", "true", "yes", "on", "开启"}
    except SQLAlchemyError:
        return bool(resolved.manage_samba_accounts)


def privileged_command(command: list[str]) -> list[str]:
    """服务非 root 运行时通过 sudo -n 执行 Samba 命令，避免 Web 请求卡在交互式密码提示。"""
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() == 0:
        return command
    return ["sudo", "-n", *command]
