import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.errors import validation_error

_ACCOUNT_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class LinuxAccountPlan:
    """记录账户操作结果；开发环境默认跳过真实系统命令，避免误改本机账户。"""

    account_name: str
    home_path: str
    executed: bool
    commands: list[list[str]]


def linux_account_for_role(username: str, role: str, settings: Settings | None = None) -> str | None:
    """根据平台角色决定 Linux 账户：管理员映射主账户，学生和导师使用独立子账户。"""
    resolved = settings or get_settings()
    if role == "admin":
        return resolved.main_linux_user
    if role in {"student", "mentor"}:
        validate_child_account_name(username, resolved)
        return username
    return None


def home_path_for_user(username: str, role: str, settings: Settings | None = None) -> str:
    """计算平台展示的 home 路径；管理员对应主账户，普通教学账户按用户名映射共享目录。"""
    resolved = settings or get_settings()
    if role == "admin":
        return f"/home/{resolved.main_linux_user}"
    return f"{resolved.user_home_root}/{username}"


def create_child_account(username: str, _user_id: int, role: str, password: str | None = None) -> LinuxAccountPlan:
    """创建学生或导师 Linux 子账户，并设置主账户 ACL 与日志目录只读访问基线。"""
    settings = get_settings()
    account_name = linux_account_for_role(username, role, settings)
    home_path = home_path_for_user(username, role, settings)
    if account_name is None or account_name == settings.main_linux_user:
        return LinuxAccountPlan(account_name=settings.main_linux_user, home_path=home_path, executed=False, commands=[])

    commands = build_create_account_commands(account_name, home_path, settings, set_password=password is not None)
    return run_account_commands(account_name, home_path, commands, settings, password=password)


def delete_child_account(username: str, role: str) -> LinuxAccountPlan:
    """删除学生或导师 Linux 子账户；管理员主账户绝不由用户删除流程移除。"""
    settings = get_settings()
    account_name = linux_account_for_role(username, role, settings)
    if account_name is None or account_name == settings.main_linux_user:
        return LinuxAccountPlan(account_name=settings.main_linux_user, home_path="", executed=False, commands=[])
    validate_child_account_name(account_name, settings)
    commands = [["userdel", "--remove", account_name]]
    return run_account_commands(account_name, "", commands, settings)


def validate_child_account_name(username: str, settings: Settings) -> None:
    """限制子账户名，避免把平台输入带入系统账户命令时产生歧义或越权目标。"""
    if username == settings.main_linux_user:
        raise validation_error("child account cannot use main linux account name")
    if not _ACCOUNT_NAME_PATTERN.fullmatch(username):
        raise validation_error("linux account name must match ^[a-z_][a-z0-9_-]{0,31}$")


def build_create_account_commands(
    account_name: str,
    home_path: str,
    settings: Settings,
    set_password: bool = False,
) -> list[list[str]]:
    """生成创建账户和权限修正命令，主账户通过 ACL 获得 rwx，目录模式仍保持 755。"""
    commands = [
        ["mkdir", "-p", settings.user_home_root, f"{settings.data_root}/logs", settings.task_log_root, settings.env_install_log_root],
        ["useradd", "--create-home", "--home-dir", home_path, "--shell", "/bin/bash", account_name],
        ["chmod", "755", home_path],
        ["chmod", "755", settings.data_root, settings.user_home_root, f"{settings.data_root}/logs", settings.task_log_root, settings.env_install_log_root],
        ["find", f"{settings.data_root}/logs", "-type", "d", "-exec", "chmod", "755", "{}", "+"],
        ["find", f"{settings.data_root}/logs", "-type", "f", "-exec", "chmod", "644", "{}", "+"],
    ]
    if set_password:
        commands.append(["chpasswd", account_name, "<redacted>"])
    if shutil.which("setfacl"):
        # 主账户需要维护用户文件；ACL 提供 rwx，不改变对用户可见的 755 基线。
        commands.extend(
            [
                ["setfacl", "-R", "-m", f"u:{settings.main_linux_user}:rwx", home_path],
                ["setfacl", "-d", "-m", f"u:{settings.main_linux_user}:rwx", home_path],
                ["setfacl", "-R", "-m", "o::rX", f"{settings.data_root}/logs"],
                ["setfacl", "-R", "-d", "-m", "o::rX", f"{settings.data_root}/logs"],
            ]
        )
    return commands


def run_account_commands(
    account_name: str,
    home_path: str,
    commands: list[list[str]],
    settings: Settings,
    password: str | None = None,
) -> LinuxAccountPlan:
    """按配置执行系统账户命令；未显式启用时只返回计划，便于测试和本地开发。"""
    if not settings.manage_linux_accounts or os.name != "posix":
        return LinuxAccountPlan(account_name=account_name, home_path=home_path, executed=False, commands=commands)
    for command in commands:
        try:
            if command[0] == "chpasswd":
                subprocess.run(["chpasswd"], input=f"{account_name}:{password}", text=True, check=True)
            else:
                subprocess.run(command, check=True)
        except subprocess.CalledProcessError as exc:
            raise validation_error("linux account command failed", data={"command": command, "returncode": exc.returncode}) from exc
        except OSError as exc:
            raise validation_error("linux account command unavailable", data={"command": command, "error": str(exc)}) from exc
    return LinuxAccountPlan(account_name=account_name, home_path=home_path, executed=True, commands=commands)
