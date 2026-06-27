from __future__ import annotations

import argparse
import json
import shutil
import psutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


IGNORED_INTERFACE_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap")


def read_cpu_counters() -> tuple[int, int]:
    """Read total and idle CPU counters from /proc/stat for delta sampling."""
    parts = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    values = [int(value) for value in parts]
    idle = values[3] + values[4]
    return sum(values), idle


def collect_cpu_usage() -> int:
    """Collect short-window CPU usage; failures return 0 so heartbeat can continue."""
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
    """Read available memory from /proc/meminfo and convert it to MB."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def collect_network_metrics(interval_seconds: float = 1.0) -> dict[str, int]:
    """Collect active NIC capacity and short-window throughput in Mbps.

    The script runs on compute nodes where psutil is not guaranteed, so it
    mirrors the ddltm 2.0 behavior by using Linux kernel counters directly:
    /sys/class/net/<iface>/speed for bandwidth and /proc/net/dev for traffic.
    """
    interfaces = collect_network_interfaces()
    first_counters = read_network_counters()
    time.sleep(max(0.1, interval_seconds))
    second_counters = read_network_counters()
    elapsed = max(0.1, interval_seconds)
    if_info = psutil.net_if_stats()

    upload_mbps = 0
    download_mbps = 0
    bandwidth_mbps = 0
    for if_stat in if_info:
        if if_info[if_stat].speed > 0:
            bandwidth_mbps = if_info[if_stat].speed
    for name, speed_mbps in interfaces.items():
        first = first_counters.get(name)
        second = second_counters.get(name)
        if first is None or second is None:
            continue
        download_mbps += bytes_to_mbps(max(0, second["rx_bytes"] - first["rx_bytes"]), elapsed)
        upload_mbps += bytes_to_mbps(max(0, second["tx_bytes"] - first["tx_bytes"]), elapsed)
    return {
        "network_bandwidth_mbps": bandwidth_mbps,
        "upload_mbps": upload_mbps,
        "download_mbps": download_mbps,
    }


def collect_network_interfaces() -> dict[str, int]:
    """Return usable network interfaces with positive link speeds in Mbps."""
    interfaces: dict[str, int] = {}
    net_root = Path("/sys/class/net")
    try:
        candidates = list(net_root.iterdir())
    except OSError:
        return interfaces
    for interface_path in candidates:
        name = interface_path.name
        if should_ignore_interface(name):
            continue
        try:
            operstate = (interface_path / "operstate").read_text(encoding="utf-8").strip()
            speed_mbps = int((interface_path / "speed").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if operstate == "up" and speed_mbps > 0:
            interfaces[name] = speed_mbps
    return interfaces


def should_ignore_interface(name: str) -> bool:
    """Filter loopback and common virtual interfaces from node capacity totals."""
    return name.startswith(IGNORED_INTERFACE_PREFIXES)


def read_network_counters() -> dict[str, dict[str, int]]:
    """Read rx/tx byte counters from /proc/net/dev for each interface."""
    counters: dict[str, dict[str, int]] = {}
    try:
        lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
    except OSError:
        return counters
    for line in lines:
        if ":" not in line:
            continue
        name, values_text = line.split(":", maxsplit=1)
        values = values_text.split()
        if len(values) < 16:
            continue
        try:
            counters[name.strip()] = {"rx_bytes": int(values[0]), "tx_bytes": int(values[8])}
        except ValueError:
            continue
    return counters


def bytes_to_mbps(byte_delta: int, elapsed_seconds: float) -> int:
    """Convert a byte delta over a sampling window to rounded megabits/second."""
    return max(0, round(byte_delta * 8 / elapsed_seconds / 1_000_000))


def collect_gpu_summary() -> tuple[list[dict[str, int | str]], bool]:
    """采集 GPU 清单；probe_ok 用来避免 nvidia-smi 短暂失败时清空 master 侧库存。"""
    if shutil.which("nvidia-smi") is None:
        return [], False
    process_counts = collect_gpu_process_counts()
    command = [
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.total,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.check_output(command, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return [], False
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
    return rows, True


def collect_gpu_process_counts() -> dict[str, int]:
    """Count compute processes per GPU UUID for scheduling visibility."""
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


def parse_args() -> argparse.Namespace:
    """解析远端监控参数；默认单次输出，长连接由 master 通过 --loop 显式启用。"""
    parser = argparse.ArgumentParser(description="Collect NebulaGrid node monitor metrics")
    parser.add_argument("--loop", action="store_true", help="keep printing one JSON snapshot per interval")
    parser.add_argument(
        "-l",
        "--interval",
        type=float,
        default=5.0,
        help="monitor output interval in seconds when --loop is enabled",
    )
    return parser.parse_args()


def normalize_interval_seconds(value: float) -> float:
    """限制远端循环周期下限，避免配置错误导致监控脚本在计算节点上空转。"""
    try:
        interval = float(value)
    except (TypeError, ValueError):
        interval = 5.0
    return max(1.0, interval)


def collect_monitor_snapshot(network_interval_seconds: float = 1.0) -> dict[str, Any]:
    """采集一帧节点监控快照；字段结构保持与 master 侧写入逻辑一致。"""
    network_metrics: dict[str, Any] = collect_network_metrics(network_interval_seconds)
    gpus, gpu_probe_ok = collect_gpu_summary()
    return {
        "cpu_usage": collect_cpu_usage(),
        "avail_ram_mb": collect_available_ram_mb(),
        **network_metrics,
        "gpus": gpus,
        "gpu_probe_ok": gpu_probe_ok,
    }


def print_snapshot(payload: dict[str, Any]) -> None:
    """按 JSON Lines 输出监控结果，flush 确保 SSH 长连接实时传回 master。"""
    print(json.dumps(payload), flush=True)


def run_loop(interval_seconds: float) -> None:
    """按固定节拍持续输出监控快照；采集耗时会从下一次等待时间中扣除。"""
    next_tick = time.monotonic()
    network_interval_seconds = min(1.0, max(0.1, interval_seconds / 2))
    while True:
        next_tick += interval_seconds
        try:
            print_snapshot(collect_monitor_snapshot(network_interval_seconds))
        except Exception as exc:  # noqa: BLE001 - 单帧采集失败不应让 SSH 监控连接直接退出。
            print_snapshot(
                {
                    "cpu_usage": 0,
                    "avail_ram_mb": 0,
                    "network_bandwidth_mbps": 0,
                    "upload_mbps": 0,
                    "download_mbps": 0,
                    "gpus": [],
                    "gpu_probe_ok": False,
                    "monitor_error": str(exc),
                }
            )
        sleep_seconds = next_tick - time.monotonic()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def main() -> None:
    """远端监控入口；单次模式用于自检，循环模式用于 master 长连接线程。"""
    args = parse_args()
    interval_seconds = normalize_interval_seconds(args.interval)
    if args.loop:
        run_loop(interval_seconds)
        return
    try:
        print_snapshot(collect_monitor_snapshot())
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
