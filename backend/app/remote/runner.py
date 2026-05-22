import argparse
import json
import os
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """解析远端任务启动参数，runner 只接收调度器生成的受控字段。"""
    parser = argparse.ArgumentParser(description="NebulaGrid remote task runner")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--runtime-path", required=True)
    parser.add_argument("--status-path", required=True)
    parser.add_argument("--cuda-visible-devices", default="")
    return parser.parse_args()


def main() -> None:
    """后台启动任务 wrapper，并写入 PID 元数据供 master 侧 executor/guard 追踪。"""
    args = parse_args()
    log_path = Path(args.log_path)
    runtime_path = Path(args.runtime_path)
    status_path = Path(args.status_path)
    wrapper_path = runtime_path.with_suffix(".sh")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(build_wrapper(args), encoding="utf-8")
    wrapper_path.chmod(0o700)
    env = os.environ.copy()
    if args.cuda_visible_devices:
        env["CUDA_VISIBLE_DEVICES"] = args.cuda_visible_devices
    else:
        # CPU-only 任务显式隐藏 GPU，避免框架默认探测全部设备。
        env["CUDA_VISIBLE_DEVICES"] = ""
    process = subprocess.Popen(
        ["/bin/bash", str(wrapper_path)],
        cwd=args.workdir,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    metadata = {
        "pid": process.pid,
        "pgid": os.getpgid(process.pid),
        "workdir": args.workdir,
        "log_path": args.log_path,
        "runtime_path": args.runtime_path,
        "status_path": args.status_path,
    }
    runtime_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False))


def build_wrapper(args: argparse.Namespace) -> str:
    """生成等待用户命令完成的 wrapper，退出码会写入 status 文件。"""
    status_path = shell_quote(args.status_path)
    log_path = shell_quote(args.log_path)
    command = shell_quote(args.command)
    return f"""#!/bin/bash
set +e
mkdir -p "$(dirname {log_path})" "$(dirname {status_path})"
echo "[NebulaGrid] task started at $(date --iso-8601=seconds)" >> {log_path}
NEBULAGRID_COMMAND={command} NEBULAGRID_LOG_PATH={log_path} python3 - <<'PY' 2>> {log_path}
import errno
import os
import pty
import select
import subprocess

command = os.environ["NEBULAGRID_COMMAND"]
log_path = os.environ["NEBULAGRID_LOG_PATH"]

# 这里不用普通 shell 重定向，而是让用户命令跑在伪终端里，再按 chunk 立即写入日志。
# 这样 Python、训练框架和进度条通常会按交互式输出刷新，避免等缓冲区满后才一次性落盘。
master_fd, slave_fd = pty.openpty()
process = subprocess.Popen(
    ["/bin/bash", "-lc", command],
    stdin=slave_fd,
    stdout=slave_fd,
    stderr=slave_fd,
    close_fds=True,
)
os.close(slave_fd)
log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_SYNC", 0), 0o644)
try:
    while True:
        ready, _, _ = select.select([master_fd], [], [], 0.2)
        if master_fd in ready:
            try:
                data = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                data = b""
            if data:
                os.write(log_fd, data)
            elif process.poll() is not None:
                break
        if process.poll() is not None:
            try:
                while True:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    os.write(log_fd, data)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
            break
finally:
    os.close(log_fd)
    os.close(master_fd)
raise SystemExit(process.wait())
PY
code=$?
echo "[NebulaGrid] task finished at $(date --iso-8601=seconds) with code $code" >> {log_path}
NEBULAGRID_RETURN_CODE=$code python3 - <<'PY' > {status_path}
import json, os, time
print(json.dumps({{"return_code": int(os.environ.get("NEBULAGRID_RETURN_CODE", "0")), "finished_at": time.time()}}, ensure_ascii=False))
PY
exit $code
"""


def shell_quote(value: str) -> str:
    """轻量 shell 引号，避免日志路径中空格破坏 wrapper。"""
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    main()
