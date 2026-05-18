import argparse
import json
import os
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """解析远端任务启动参数，保持 runner 只接收受控字段。"""
    parser = argparse.ArgumentParser(description="NebulaGrid remote task runner")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--runtime-path", required=True)
    parser.add_argument("--cuda-visible-devices", default="")
    return parser.parse_args()


def main() -> None:
    """启动用户命令并写入 PID 元数据，供 master 侧 executor/guard 追踪。"""
    args = parse_args()
    Path(args.log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(args.runtime_path).parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if args.cuda_visible_devices:
        env["CUDA_VISIBLE_DEVICES"] = args.cuda_visible_devices
    with open(args.log_path, "ab") as log_file:
        process = subprocess.Popen(
            args.command,
            cwd=args.workdir,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            shell=True,
            start_new_session=True,
        )
    metadata = {"pid": process.pid, "workdir": args.workdir, "log_path": args.log_path}
    Path(args.runtime_path).write_text(json.dumps(metadata), encoding="utf-8")
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()

