from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path


def read_cpu_counters() -> tuple[int, int]:
    """读取 /proc/stat 的总量和 idle 计数，用两次采样估算 CPU 利用率。"""
    parts = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    values = [int(value) for value in parts]
    idle = values[3] + values[4]
    return sum(values), idle


def collect_cpu_usage() -> int:
    """采集短窗口 CPU 利用率；读取失败时返回 0 避免影响节点在线判断。"""
    try:
        total_1, idle_1 = read_cpu_counters()
        time.sleep(0.2)
        total_2, idle_2 = read_cpu_counters()
    except (OSError, ValueError, IndexError):
        return 0
    total_delta = max(1, total_2 - total_1)
    idle_delta = max(0, idle_2 - idle_1)
    return max(0, min(100, round((1 - idle_delta / total_delta) * 100)))


def collect_available_ram_mb() -> int:
    """从 /proc/meminfo 读取可用内存，单位转换为 MB。"""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def collect_gpu_summary() -> list[dict[str, int | str]]:
    """调用 nvidia-smi 采集 GPU 摘要，不可用时返回空列表。"""
    if shutil.which("nvidia-smi") is None:
        return []
    process_counts = collect_gpu_process_counts()
    command = [
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.total,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.check_output(command, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return []
    rows = []
    for line in output.splitlines():
        index, uuid, name, total, free, util = [part.strip() for part in line.split(",", maxsplit=5)]
        rows.append(
            {
                "index": int(index),
                "uuid": uuid,
                "name": name,
                "memory_total_mb": int(total),
                "memory_free_mb": int(free),
                "gpu_usage": int(util),
                "process_count": process_counts.get(uuid, 0),
            }
        )
    return rows


def collect_gpu_process_counts() -> dict[str, int]:
    """统计每张 GPU 上正在运行的 compute 进程数量，用于判断 GPU 是否被调用。"""
    command = [
        "nvidia-smi",
        "--query-compute-apps=gpu_uuid,pid",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return {}
    counts: dict[str, int] = {}
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",", maxsplit=1)]
        if len(parts) != 2 or not parts[0] or parts[0] == "[Not Supported]":
            continue
        counts[parts[0]] = counts.get(parts[0], 0) + 1
    return counts


def main() -> None:
    """输出 JSON 格式节点监控数据，供 master 侧 SSH 调用解析。"""
    print(
        json.dumps(
            {
                "cpu_usage": collect_cpu_usage(),
                "avail_ram_mb": collect_available_ram_mb(),
                "upload_mbps": 0,
                "download_mbps": 0,
                "gpus": collect_gpu_summary(),
            }
        )
    )


if __name__ == "__main__":
    main()
