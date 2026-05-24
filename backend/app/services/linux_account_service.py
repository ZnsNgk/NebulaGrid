import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.errors import validation_error

try:
    import pwd
except ImportError:  # pragma: no cover - Windows 开发环境没有 pwd 模块。
    pwd = None

_ACCOUNT_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_KNOWN_COMMAND_PATHS = {
    "chmod": ("/usr/bin/chmod", "/bin/chmod"),
    "chown": ("/usr/bin/chown", "/bin/chown"),
    "chpasswd": ("/usr/sbin/chpasswd", "/sbin/chpasswd"),
    "find": ("/usr/bin/find", "/bin/find"),
    "mkdir": ("/usr/bin/mkdir", "/bin/mkdir"),
    "setfacl": ("/usr/bin/setfacl", "/bin/setfacl"),
    "useradd": ("/usr/sbin/useradd", "/sbin/useradd"),
    "userdel": ("/usr/sbin/userdel", "/sbin/userdel"),
    "usermod": ("/usr/sbin/usermod", "/sbin/usermod"),
}


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
    if username == resolved.main_linux_user:
        return resolved.main_linux_user
    if role in {"admin", "student", "mentor"}:
        validate_child_account_name(username, resolved)
        return username
    return None


def home_path_for_user(username: str, role: str, settings: Settings | None = None) -> str:
    """计算平台展示的 home 路径；管理员对应主账户，普通教学账户按用户名映射共享目录。"""
    resolved = settings or get_settings()
    if username == resolved.main_linux_user:
        return f"/home/{resolved.main_linux_user}"
    return f"{resolved.user_home_root}/{username}"


def ensure_managed_home_directory(home_path: str, settings: Settings | None = None) -> bool:
    """确保教学用户文件根目录存在，并按实验室共享策略设置为 755。"""
    resolved = settings or get_settings()
    user_home_root = Path(resolved.user_home_root).resolve(strict=False)
    target = Path(home_path).resolve(strict=False)
    if target == user_home_root or user_home_root not in target.parents:
        return False
    target.mkdir(parents=True, exist_ok=True)
    try:
        # 用户根目录先按最小权限创建；后续账户维护命令再用 ACL 单独放开主账号访问。
        target.chmod(0o700)
    except OSError:
        # 某些 NFS/Windows 开发环境不支持 chmod；目录已经存在即可满足文件管理入口。
        pass
    return True


def ensure_child_account(username: str, role: str, password: str | None = None) -> LinuxAccountPlan:
    """确保学生或导师可用同名 SSH 账户登录自己的工作目录。"""
    settings = get_settings()
    account_name = linux_account_for_role(username, role, settings)
    home_path = home_path_for_user(username, role, settings)
    if account_name is None or account_name == settings.main_linux_user:
        return LinuxAccountPlan(account_name=settings.main_linux_user, home_path=home_path, executed=False, commands=[])

    ensure_managed_home_directory(home_path, settings)
    commands = build_ensure_account_commands(account_name, home_path, settings, account_exists(account_name), password is not None)
    return run_account_commands(account_name, home_path, commands, settings, password=password)


def create_child_account(username: str, _user_id: int, role: str, password: str | None = None) -> LinuxAccountPlan:
    """创建或复用学生/导师 Linux 子账户，并把密码同步为平台密码。"""
    return ensure_child_account(username, role, password=password)


def delete_child_account(username: str, role: str) -> LinuxAccountPlan:
    """删除学生或导师 Linux 子账户；管理员主账户绝不由用户删除流程移除。"""
    settings = get_settings()
    account_name = linux_account_for_role(username, role, settings)
    if account_name is None or account_name == settings.main_linux_user:
        return LinuxAccountPlan(account_name=settings.main_linux_user, home_path="", executed=False, commands=[])
    validate_child_account_name(account_name, settings)
    if not account_exists(account_name):
        return LinuxAccountPlan(account_name=account_name, home_path="", executed=False, commands=[])
    commands = [[command_path("userdel"), "--remove", account_name]]
    return run_account_commands(account_name, "", commands, settings)


def set_child_account_password(username: str, role: str, password: str) -> LinuxAccountPlan:
    """把平台密码同步到对应 SSH 账户；管理员主账户不在平台内改系统密码。"""
    settings = get_settings()
    account_name = linux_account_for_role(username, role, settings)
    home_path = home_path_for_user(username, role, settings)
    if account_name is None or account_name == settings.main_linux_user:
        return LinuxAccountPlan(account_name=settings.main_linux_user, home_path=home_path, executed=False, commands=[])
    validate_child_account_name(account_name, settings)
    commands = []
    if not account_exists(account_name):
        commands.extend(build_ensure_account_commands(account_name, home_path, settings, exists=False, set_password=False))
    commands.append([command_path("chpasswd"), account_name, "<redacted>"])
    return run_account_commands(account_name, home_path, commands, settings, password=password)


def account_exists(account_name: str) -> bool:
    """检查本机是否已经存在 Linux 账户；非 POSIX 开发环境永远按不存在处理。"""
    if os.name != "posix" or pwd is None:
        return False
    try:
        pwd.getpwnam(account_name)
        return True
    except KeyError:
        return False


def validate_child_account_name(username: str, settings: Settings) -> None:
    """限制子账户名，避免把平台输入带入系统账户命令时产生歧义或越权目标。"""
    if not _ACCOUNT_NAME_PATTERN.fullmatch(username):
        raise validation_error("linux account name must match ^[a-z_][a-z0-9_-]{0,31}$")


def build_ensure_account_commands(
    account_name: str,
    home_path: str,
    settings: Settings,
    exists: bool,
    set_password: bool,
) -> list[list[str]]:
    """生成幂等账户维护命令，确保 SSH 登录目录和共享权限符合平台约定。"""
    commands: list[list[str]] = [
        [command_path("mkdir"), "-p", settings.user_home_root, f"{settings.data_root}/logs", settings.task_log_root, settings.env_install_log_root],
    ]
    if exists:
        commands.append([command_path("usermod"), "--home", home_path, "--shell", "/bin/bash", account_name])
        commands.append([command_path("mkdir"), "-p", home_path])
    else:
        commands.append([command_path("useradd"), "--create-home", "--home-dir", home_path, "--shell", "/bin/bash", account_name])
    commands.extend(build_permission_commands(account_name, home_path, settings))
    if set_password:
        commands.append([command_path("chpasswd"), account_name, "<redacted>"])
    return commands


def build_permission_commands(account_name: str, home_path: str, settings: Settings) -> list[list[str]]:
    """按实验室共享策略设置目录权限：用户之间可读可进入，平台主账户可维护。"""
    commands = [
        [command_path("chmod"), "755", settings.data_root, settings.user_home_root, f"{settings.data_root}/logs", settings.task_log_root, settings.env_install_log_root],
        [command_path("chown"), "-R", f"{account_name}:{account_name}", home_path],
        [command_path("chmod"), "-R", "u+rwX,go-rwx", home_path],
        [command_path("find"), f"{settings.data_root}/logs", "-type", "d", "-exec", command_path("chmod"), "755", "{}", "+"],
        [command_path("find"), f"{settings.data_root}/logs", "-type", "f", "-exec", command_path("chmod"), "644", "{}", "+"],
    ]
    setfacl = command_path("setfacl")
    if command_exists("setfacl"):
        commands.extend(
            [
                # 主账号和目录用户需要双向写入；other 关闭，避免不同子账号互相操作文件。
                [setfacl, "-R", "-m", f"u:{account_name}:rwX,u:{settings.main_linux_user}:rwX,m::rwx,o::---", home_path],
                [
                    command_path("find"),
                    home_path,
                    "-type",
                    "d",
                    "-exec",
                    setfacl,
                    "-d",
                    "-m",
                    f"u:{account_name}:rwx,u:{settings.main_linux_user}:rwx,g::---,m::rwx,o::---",
                    "{}",
                    "+",
                ],
                [setfacl, "-R", "-m", "o::rX", f"{settings.data_root}/logs"],
                [command_path("find"), f"{settings.data_root}/logs", "-type", "d", "-exec", setfacl, "-d", "-m", "o::rX", "{}", "+"],
            ]
        )
    return commands


def command_exists(name: str) -> bool:
    """判断系统命令是否可用，便于 setfacl 这类可选增强命令安全降级。"""
    if shutil.which(name) is not None:
        return True
    return any(Path(candidate).exists() for candidate in _KNOWN_COMMAND_PATHS.get(name, ()))


def command_path(name: str) -> str:
    """返回系统命令绝对路径，便于 sudoers 精确授权；缺失时保留原名用于报错。"""
    resolved = shutil.which(name)
    if resolved:
        return resolved
    for candidate in _KNOWN_COMMAND_PATHS.get(name, ()):
        if Path(candidate).exists():
            return candidate
    return name


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
            if Path(command[0]).name == "chpasswd":
                subprocess.run(
                    privileged_command([command[0]]),
                    input=f"{account_name}:{password}\n",
                    text=True,
                    check=True,
                    capture_output=True,
                )
            else:
                subprocess.run(privileged_command(command), text=True, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            raise validation_error(
                "linux account command failed",
                data={
                    "command": command,
                    "returncode": exc.returncode,
                    "stderr": (exc.stderr or "").strip(),
                },
            ) from exc
        except OSError as exc:
            raise validation_error("linux account command unavailable", data={"command": command, "error": str(exc)}) from exc
    return LinuxAccountPlan(account_name=account_name, home_path=home_path, executed=True, commands=commands)


def privileged_command(command: list[str]) -> list[str]:
    """服务非 root 运行时通过 sudo -n 执行账户命令，避免交互式卡死。"""
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() == 0:
        return command
    return ["sudo", "-n", *command]
