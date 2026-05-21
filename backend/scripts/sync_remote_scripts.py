"""同步 NebulaGrid 远端执行脚本到主节点共享目录和计算节点。

这个脚本只同步 backend/app/remote 下的 Python 文件，避免把开发文件、
缓存目录或无关配置带到计算节点。默认先写入本机的 remote_code_root；
如果指定 --all-db-nodes 或 --host，则继续通过 ssh/scp 分发到计算节点。
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@dataclass(frozen=True)
class SyncTarget:
    """远端同步目标，host 为空表示只同步到本机目录。"""

    host: str | None
    ssh_user: str
    remote_root: str

    @property
    def label(self) -> str:
        """返回用于日志展示的目标名称。"""
        return "local" if self.host is None else f"{self.ssh_user}@{self.host}"


def parse_args() -> argparse.Namespace:
    """解析命令行参数，避免部署时误把本地开发目录当作远端目标。"""
    parser = argparse.ArgumentParser(description="Sync NebulaGrid remote/*.py scripts")
    parser.add_argument(
        "--source",
        default=str(BACKEND_ROOT / "app" / "remote"),
        help="远端脚本源目录，默认 backend/app/remote",
    )
    parser.add_argument(
        "--remote-root",
        default=os.getenv("NEBULAGRID_REMOTE_CODE_ROOT", "/home/ddltm/envs/nebulagrid_remote"),
        help="目标目录，默认读取 NEBULAGRID_REMOTE_CODE_ROOT",
    )
    parser.add_argument(
        "--ssh-user",
        default=os.getenv("NEBULAGRID_MAIN_LINUX_USER", "ddltm"),
        help="手动指定 host 时使用的 SSH 用户，默认读取 NEBULAGRID_MAIN_LINUX_USER",
    )
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="额外同步的计算节点地址，可重复指定；例如 --host 10.16.61.186",
    )
    parser.add_argument(
        "--all-db-nodes",
        action="store_true",
        help="从数据库读取已登记计算节点，并同步到每个节点的 ssh_user@ip",
    )
    parser.add_argument(
        "--skip-local",
        action="store_true",
        help="跳过本机 remote_code_root 同步，只分发到计算节点",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划执行的动作，不写文件、不执行 SSH/SCP",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="单个 SSH/SCP 命令超时时间，单位秒",
    )
    return parser.parse_args()


def collect_remote_scripts(source_dir: Path) -> list[Path]:
    """收集需要分发的脚本，只允许同步普通 .py 文件。"""
    if not source_dir.is_dir():
        raise SystemExit(f"remote source directory not found: {source_dir}")
    scripts = sorted(path for path in source_dir.glob("*.py") if path.is_file())
    if not scripts:
        raise SystemExit(f"no remote python scripts found in: {source_dir}")
    return scripts


def load_db_targets(remote_root: str) -> list[SyncTarget]:
    """从数据库读取计算节点，避免手工漏填新增节点。"""
    from sqlalchemy import select

    from app.db.models import Node
    from app.db.session import SessionLocal
    from app.services.node_service import is_control_plane_node

    with SessionLocal() as db:
        nodes = db.scalars(select(Node).order_by(Node.id)).all()
        return [
            SyncTarget(host=node.ip, ssh_user=node.ssh_user, remote_root=remote_root)
            for node in nodes
            if not is_control_plane_node(node)
        ]


def build_targets(args: argparse.Namespace) -> list[SyncTarget]:
    """合并本机、手动 host 和数据库节点目标，并按 host 去重。"""
    targets: list[SyncTarget] = []
    if not args.skip_local:
        targets.append(SyncTarget(host=None, ssh_user=args.ssh_user, remote_root=args.remote_root))
    targets.extend(SyncTarget(host=host, ssh_user=args.ssh_user, remote_root=args.remote_root) for host in args.host)
    if args.all_db_nodes:
        targets.extend(load_db_targets(args.remote_root))

    deduped: list[SyncTarget] = []
    seen: set[tuple[str | None, str, str]] = set()
    for target in targets:
        key = (target.host, target.ssh_user, target.remote_root)
        if key not in seen:
            deduped.append(target)
            seen.add(key)
    return deduped


def sync_local(scripts: list[Path], target: SyncTarget, dry_run: bool) -> None:
    """同步脚本到主节点本机目录，适合 remote_code_root 位于 NFS 共享目录的部署。"""
    target_dir = Path(target.remote_root)
    print(f"[local] {len(scripts)} files -> {target_dir}")
    if dry_run:
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    for script in scripts:
        dest = target_dir / script.name
        shutil.copy2(script, dest)
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def shell_quote(value: str) -> str:
    """为远端 POSIX shell 参数加引号，避免路径中的特殊字符改变命令语义。"""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def run_command(args: list[str], dry_run: bool, timeout: int) -> None:
    """执行外部命令；dry-run 时只输出命令，便于上线前审查分发范围。"""
    print("+ " + " ".join(args))
    if dry_run:
        return
    subprocess.run(args, check=True, timeout=timeout)


def sync_remote(scripts: list[Path], target: SyncTarget, dry_run: bool, timeout: int) -> None:
    """通过 SSH/SCP 同步脚本到单个计算节点。"""
    if target.host is None:
        raise ValueError("remote sync target requires host")
    login = f"{target.ssh_user}@{target.host}"
    quoted_root = shell_quote(target.remote_root)
    run_command(["ssh", login, f"mkdir -p {quoted_root} && chmod 755 {quoted_root}"], dry_run, timeout)
    run_command(["scp", "-p", *[str(script) for script in scripts], f"{login}:{target.remote_root.rstrip('/')}/"], dry_run, timeout)
    run_command(["ssh", login, f"chmod 755 {quoted_root}/*.py"], dry_run, timeout)


def main() -> int:
    """同步入口，逐个目标执行，任意节点失败时返回非零状态供部署脚本感知。"""
    args = parse_args()
    scripts = collect_remote_scripts(Path(args.source).resolve())
    targets = build_targets(args)
    if not targets:
        raise SystemExit("no sync target selected; use local default, --host, or --all-db-nodes")

    print("remote scripts:")
    for script in scripts:
        print(f"  - {script.name}")
    print("targets:")
    for target in targets:
        print(f"  - {target.label}:{target.remote_root}")

    for target in targets:
        if target.host is None:
            sync_local(scripts, target, args.dry_run)
        else:
            sync_remote(scripts, target, args.dry_run, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
