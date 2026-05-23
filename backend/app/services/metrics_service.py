from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class NodeMetricSnapshot:
    """InfluxDB 中节点最新监控快照，供节点列表展示当前状态。"""

    cpu_usage: int | None = None
    avail_ram_mb: int | None = None
    network_bandwidth_mbps: int | None = None
    upload_mbps: int | None = None
    download_mbps: int | None = None
    collected_at: str | None = None


@dataclass(frozen=True)
class GpuMetricSnapshot:
    """InfluxDB 中 GPU 最新监控快照，供节点列表展示当前状态。"""

    gpu_usage: int | None = None
    free_vram_mb: int | None = None
    process_count: int | None = None
    collected_at: str | None = None


@dataclass(frozen=True)
class LatestMetrics:
    """按节点和 GPU ID 组织的最新监控快照。"""

    nodes: dict[int, NodeMetricSnapshot] = field(default_factory=dict)
    gpus: dict[int, GpuMetricSnapshot] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricPoint:
    """展示大屏历史曲线中的单个采样点，时间和值都保持轻量结构便于前端绘制。"""

    time: str
    value: int


@dataclass(frozen=True)
class HistoricalMetrics:
    """按节点和 GPU ID 归档的 InfluxDB 历史监控序列。"""

    nodes: dict[int, dict[str, list[MetricPoint]]] = field(default_factory=dict)
    gpus: dict[int, dict[str, list[MetricPoint]]] = field(default_factory=dict)


def write_monitor_snapshot(node: Any, payload: dict[str, Any], gpu_rows: list[tuple[Any, dict[str, Any]]]) -> None:
    """把一轮节点监控结果写入 InfluxDB，PostgreSQL 只保留节点和 GPU 清单。"""
    settings = get_settings()
    if not influx_enabled(settings):
        return
    timestamp = time.time_ns()
    lines = [
        build_line(
            "node_metrics",
            {"node_id": node.id, "node_name": node.name, "ip": node.ip, "gpu_id": "none"},
            {
                "cpu_usage": coerce_int(payload.get("cpu_usage")),
                "avail_ram_mb": coerce_int(payload.get("avail_ram_mb")),
                "network_bandwidth_mbps": coerce_int(payload.get("network_bandwidth_mbps")),
                "upload_mbps": coerce_int(payload.get("upload_mbps")),
                "download_mbps": coerce_int(payload.get("download_mbps")),
            },
            timestamp,
        )
    ]
    for gpu, item in gpu_rows:
        lines.append(
            build_line(
                "gpu_metrics",
                {
                    "node_id": node.id,
                    "node_name": node.name,
                    "gpu_id": gpu.id,
                    "gpu_uuid": item.get("uuid", ""),
                    "gpu_index": gpu.gpu_index,
                    "model": gpu.model,
                },
                {
                    "gpu_usage": coerce_int(item.get("gpu_usage")),
                    "free_vram_mb": coerce_int(item.get("memory_free_mb")),
                    "process_count": coerce_int(item.get("process_count")),
                    "called": 1 if coerce_int(item.get("process_count")) > 0 else 0,
                },
                timestamp,
            )
        )
    write_lines(settings, lines)


def get_latest_metrics(node_ids: list[int], gpu_ids: list[int]) -> LatestMetrics:
    """从 InfluxDB 读取指定节点和 GPU 的最新快照；未配置时返回空快照。"""
    settings = get_settings()
    if not influx_enabled(settings) or (not node_ids and not gpu_ids):
        return LatestMetrics()
    flux = build_latest_query(settings, node_ids, gpu_ids)
    rows = query_flux(settings, flux)
    node_values: dict[int, dict[str, Any]] = {}
    gpu_values: dict[int, dict[str, Any]] = {}
    for row in rows:
        measurement = row.get("_measurement")
        field_name = row.get("_field")
        value = row.get("_value")
        collected_at = row.get("_time") or None
        if measurement == "node_metrics":
            node_id = coerce_int(row.get("node_id"))
            values = node_values.setdefault(node_id, {})
            values[field_name] = coerce_int(value)
            values["collected_at"] = collected_at
        elif measurement == "gpu_metrics":
            gpu_id = coerce_int(row.get("gpu_id"))
            values = gpu_values.setdefault(gpu_id, {})
            values[field_name] = coerce_int(value)
            values["collected_at"] = collected_at
    return LatestMetrics(
        nodes={
            node_id: NodeMetricSnapshot(
                cpu_usage=values.get("cpu_usage"),
                avail_ram_mb=values.get("avail_ram_mb"),
                network_bandwidth_mbps=values.get("network_bandwidth_mbps"),
                upload_mbps=values.get("upload_mbps"),
                download_mbps=values.get("download_mbps"),
                collected_at=values.get("collected_at"),
            )
            for node_id, values in node_values.items()
        },
        gpus={
            gpu_id: GpuMetricSnapshot(
                gpu_usage=values.get("gpu_usage"),
                free_vram_mb=values.get("free_vram_mb"),
                process_count=values.get("process_count"),
                collected_at=values.get("collected_at"),
            )
            for gpu_id, values in gpu_values.items()
        },
    )


def get_historical_metrics(node_ids: list[int], gpu_ids: list[int]) -> HistoricalMetrics:
    """读取展示大屏需要的历史监控曲线；InfluxDB 未配置时返回空序列，不影响页面展示。"""
    settings = get_settings()
    if not influx_enabled(settings) or (not node_ids and not gpu_ids):
        return HistoricalMetrics()
    flux = build_history_query(settings, node_ids, gpu_ids)
    rows = query_flux(settings, flux)
    node_values: dict[int, dict[str, list[MetricPoint]]] = {}
    gpu_values: dict[int, dict[str, list[MetricPoint]]] = {}
    for row in rows:
        measurement = row.get("_measurement")
        field_name = row.get("_field") or ""
        if field_name not in {
            "cpu_usage",
            "avail_ram_mb",
            "upload_mbps",
            "download_mbps",
            "gpu_usage",
            "free_vram_mb",
            "process_count",
        }:
            continue
        point = MetricPoint(time=row.get("_time") or "", value=coerce_int(row.get("_value")))
        if measurement == "node_metrics":
            node_id = coerce_int(row.get("node_id"))
            node_values.setdefault(node_id, {}).setdefault(field_name, []).append(point)
        elif measurement == "gpu_metrics":
            gpu_id = coerce_int(row.get("gpu_id"))
            gpu_values.setdefault(gpu_id, {}).setdefault(field_name, []).append(point)
    return HistoricalMetrics(nodes=node_values, gpus=gpu_values)


def build_latest_query(settings: Settings, node_ids: list[int], gpu_ids: list[int]) -> str:
    """构造读取节点/GPU 最新值的 Flux 查询。"""
    node_filter = " or ".join(f'r.node_id == "{node_id}"' for node_id in node_ids) or "false"
    gpu_filter = " or ".join(f'r.gpu_id == "{gpu_id}"' for gpu_id in gpu_ids) or "false"
    return f'''
from(bucket: "{escape_flux_string(settings.influxdb_bucket)}")
  |> range(start: -{settings.influxdb_latest_range})
  |> filter(fn: (r) =>
    (r._measurement == "node_metrics" and ({node_filter})) or
    (r._measurement == "gpu_metrics" and ({gpu_filter}))
  )
  |> group(columns: ["_measurement", "node_id", "gpu_id", "_field"])
  |> last()
'''


def build_history_query(settings: Settings, node_ids: list[int], gpu_ids: list[int]) -> str:
    """构造展示大屏历史曲线 Flux 查询，按配置窗口聚合以限制响应体大小。"""
    node_filter = " or ".join(f'r.node_id == "{node_id}"' for node_id in node_ids) or "false"
    gpu_filter = " or ".join(f'r.gpu_id == "{gpu_id}"' for gpu_id in gpu_ids) or "false"
    node_fields = (
        'r._field == "cpu_usage" or r._field == "avail_ram_mb" or '
        'r._field == "upload_mbps" or r._field == "download_mbps"'
    )
    gpu_fields = (
        'r._field == "gpu_usage" or r._field == "free_vram_mb" or '
        'r._field == "process_count"'
    )
    return f'''
from(bucket: "{escape_flux_string(settings.influxdb_bucket)}")
  |> range(start: -{settings.influxdb_presenter_range})
  |> filter(fn: (r) =>
    (r._measurement == "node_metrics" and ({node_filter}) and ({node_fields})) or
    (r._measurement == "gpu_metrics" and ({gpu_filter}) and ({gpu_fields}))
  )
  |> aggregateWindow(every: {settings.influxdb_presenter_window}, fn: mean, createEmpty: false)
  |> group(columns: ["_measurement", "node_id", "gpu_id", "_field"])
  |> sort(columns: ["_time"])
'''


def query_flux(settings: Settings, flux: str) -> list[dict[str, str]]:
    """调用 InfluxDB 查询接口并把 CSV 响应解析为字典行。"""
    params = urllib.parse.urlencode({"org": settings.influxdb_org})
    url = f"{settings.influxdb_url.rstrip('/')}/api/v2/query?{params}"
    request = urllib.request.Request(
        url,
        data=json.dumps({"query": flux}).encode("utf-8"),
        headers={
            "Authorization": f"Token {settings.influxdb_token}",
            "Accept": "application/csv",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        content = response.read().decode("utf-8")
    rows = []
    for row in csv.DictReader(line for line in content.splitlines() if line and not line.startswith("#")):
        rows.append(row)
    return rows


def write_lines(settings: Settings, lines: list[str]) -> None:
    """调用 InfluxDB 写入接口，使用 line protocol 保存监控点。"""
    params = urllib.parse.urlencode(
        {"org": settings.influxdb_org, "bucket": settings.influxdb_bucket, "precision": "ns"}
    )
    url = f"{settings.influxdb_url.rstrip('/')}/api/v2/write?{params}"
    request = urllib.request.Request(
        url,
        data="\n".join(lines).encode("utf-8"),
        headers={
            "Authorization": f"Token {settings.influxdb_token}",
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5):
        return


def build_line(measurement: str, tags: dict[str, Any], fields: dict[str, int], timestamp: int) -> str:
    """构造 InfluxDB line protocol 行，字段均按整数写入。"""
    tag_text = ",".join(f"{escape_tag(key)}={escape_tag(value)}" for key, value in tags.items())
    field_text = ",".join(f"{escape_field_key(key)}={value}i" for key, value in fields.items())
    return f"{escape_measurement(measurement)},{tag_text} {field_text} {timestamp}"


def influx_enabled(settings: Settings) -> bool:
    """只有配置了 token、org 和 bucket 时才启用 InfluxDB 读写。"""
    return bool(settings.influxdb_url and settings.influxdb_org and settings.influxdb_bucket and settings.influxdb_token)


def coerce_int(value: Any) -> int:
    """把远端脚本输出的字符串/数字安全转换为非负整数。"""
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return 0


def escape_measurement(value: Any) -> str:
    """转义 line protocol measurement。"""
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def escape_tag(value: Any) -> str:
    """转义 line protocol tag key/value。"""
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


def escape_field_key(value: Any) -> str:
    """转义 line protocol field key。"""
    return escape_tag(value)


def escape_flux_string(value: str) -> str:
    """转义 Flux 字符串字面量。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')
