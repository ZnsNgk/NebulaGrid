from app.core.config import Settings
from app.services import linux_account_service


def test_linux_account_permissions_share_owner_with_main_but_isolate_users(monkeypatch) -> None:
    """验证 Linux 子账号目录只对目录用户和主账号开放写权限，避免子账号之间互相操作文件。"""
    monkeypatch.setattr(linux_account_service, "command_exists", lambda name: name == "setfacl")
    monkeypatch.setattr(linux_account_service, "command_path", lambda name: f"/usr/bin/{name}")
    settings = Settings(
        data_root="/data",
        user_home_root="/data/user",
        task_log_root="/data/logs/task_logs",
        env_install_log_root="/data/logs/env_install_logs",
        main_linux_user="ddltm",
    )

    commands = linux_account_service.build_permission_commands("alice", "/data/user/alice", settings)

    assert ["/usr/bin/chmod", "-R", "u+rwX,go-rwx", "/data/user/alice"] in commands
    assert [
        "/usr/bin/setfacl",
        "-R",
        "-m",
        "u:alice:rwX,u:ddltm:rwX,m::rwx,o::---",
        "/data/user/alice",
    ] in commands
    assert [
        "/usr/bin/find",
        "/data/user/alice",
        "-type",
        "d",
        "-exec",
        "/usr/bin/setfacl",
        "-d",
        "-m",
        "u:alice:rwx,u:ddltm:rwx,g::---,m::rwx,o::---",
        "{}",
        "+",
    ] in commands
    assert ["/usr/bin/chmod", "-R", "755", "/data/user/alice"] not in commands
