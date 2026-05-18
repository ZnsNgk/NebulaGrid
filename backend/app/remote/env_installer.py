import argparse
import json
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """解析环境包安装参数，避免远端脚本读取非受控输入。"""
    parser = argparse.ArgumentParser(description="NebulaGrid remote environment installer")
    parser.add_argument("--python", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--log-path", required=True)
    return parser.parse_args()


def main() -> None:
    """使用指定 Python 环境安装包，并把 pip 输出写入日志。"""
    args = parse_args()
    Path(args.log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(args.log_path, "ab") as log_file:
        result = subprocess.run(
            [args.python, "-m", "pip", "install", args.package],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
        )
    print(json.dumps({"return_code": result.returncode, "log_path": args.log_path}))


if __name__ == "__main__":
    main()

