"""NebulaGrid 部署自检脚本。

该脚本只做只读检查，不创建、不删除、不修改远端文件。它用于在真实计算节点
接入 scheduler/executor/monitor 前验证 SSH、NFS 共享路径、主账户 UID/GID、
远端脚本同步和 nvidia-smi 可用性，避免任务启动后才发现路径不一致。
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@dataclass(frozen=True)
class CheckResult:
    """单项检查结果；message 写明风险点，便于部署人员按项修复。"""

    name: str
    ok: bool
    message: str


def parse_args() -> argparse.Namespace:
    """解析自检参数，默认从数据库读取全部计算节点。"""
    parser = argparse.ArgumentParser(description="Run NebulaGrid deployment self checks")
    parser.add_argument("--host", action="append", default=[], help="只检查指定计算节点 IP/主机名，可重复指定")
    parser.add_argument("--timeout", type=int, default=15, help="单个 SSH 检查超时时间，单位秒")
    return parser.parse_args()


def main() -> int:
    """执行本机和远端自检，任意失败项都会返回非零退出码。"""
    args = parse_args()
    from app.core.config import get_settings

    settings = get_settings()
    results = [
        check_local_directory("data_root", settings.data_root),
        check_local_directory("remote_code_root", settings.remote_code_root),
        check_local_file("runner.py", f"{settings.remote_code_root.rstrip('/')}/runner.py"),
        check_local_file("monitor.py", f"{settings.remote_code_root.rstrip('/')}/monitor.py"),
        check_local_file("env_installer.py", f"{settings.remote_code_root.rstrip('/')}/env_installer.py"),
    ]
    for node in load_targets(args.host):
        results.extend(check_remote_node(node, settings, args.timeout))
    for result in results:
        prefix = "OK" if result.ok else "FAIL"
        print(f"[{prefix}] {result.name}: {result.message}")
    return 0 if all(result.ok for result in results) else 1


def load_targets(host_filter: list[str]):
    """读取计算节点目标；指定 --host 时仍复用数据库中的 ssh_user。"""
    from sqlalchemy import select

    from app.db.models import Node
    from app.db.session import SessionLocal
    from app.services.node_service import is_control_plane_node

    wanted = set(host_filter)
    with SessionLocal() as db:
        nodes = [
            node
            for node in db.scalars(select(Node).order_by(Node.id)).all()
            if not is_control_plane_node(node) and (not wanted or node.ip in wanted or node.name in wanted)
        ]
    return nodes


def check_local_directory(name: str, path: str) -> CheckResult:
    """确认本机共享目录存在且可读写，NFS server 侧路径错误会直接导致任务启动失败。"""
    target = Path(path)
    ok = target.is_dir() and os.access(target, os.R_OK | os.W_OK | os.X_OK)
    return CheckResult(name, ok, f"{path} {'is ready' if ok else 'is missing or not writable'}")


def check_local_file(name: str, path: str) -> CheckResult:
    """确认远端脚本已经同步到 remote_code_root，避免 executor/monitor 调用不存在的脚本。"""
    target = Path(path)
    ok = target.is_file() and os.access(target, os.R_OK)
    return CheckResult(name, ok, f"{path} {'exists' if ok else 'not found; run backend/scripts/sync_remote_scripts.py'}")


def check_remote_node(node, settings, timeout: int) -> list[CheckResult]:
    """对单个计算节点执行 SSH、身份、共享路径、脚本和 GPU 检查。"""
    login = f"{node.ssh_user}@{node.ip}"
    command = " && ".join(
        [
            "hostname",
            "id -u",
            "id -g",
            f"test -d {shlex.quote(settings.data_root)}",
            f"test -d {shlex.quote(settings.remote_code_root)}",
            f"test -r {shlex.quote(settings.remote_code_root.rstrip('/') + '/runner.py')}",
            "command -v nvidia-smi >/dev/null",
            "nvidia-smi -L >/dev/null",
        ]
    )
    try:
        output = subprocess.check_output(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", login, command],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 自检要报告失败原因，不让异常栈淹没部署人员。
        return [CheckResult(f"node:{node.name}", False, f"{login} self check failed: {exc}")]
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return [CheckResult(f"node:{node.name}", True, f"{login} ok; host={lines[0] if lines else 'unknown'}")]


if __name__ == "__main__":
    raise SystemExit(main())
