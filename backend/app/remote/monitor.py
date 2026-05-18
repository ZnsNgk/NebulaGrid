import json
import shutil
import subprocess


def collect_gpu_summary() -> list[dict[str, str]]:
    """调用 nvidia-smi 采集 GPU 摘要，不可用时返回空列表。"""
    if shutil.which("nvidia-smi") is None:
        return []
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    output = subprocess.check_output(command, text=True, timeout=10)
    rows = []
    for line in output.splitlines():
        index, name, total, free, util = [part.strip() for part in line.split(",", maxsplit=4)]
        rows.append({"index": index, "name": name, "memory_total_mb": total, "memory_free_mb": free, "gpu_usage": util})
    return rows


def main() -> None:
    """输出 JSON 格式节点监控数据，供 master 侧 SSH 调用解析。"""
    print(json.dumps({"gpus": collect_gpu_summary()}))


if __name__ == "__main__":
    main()

