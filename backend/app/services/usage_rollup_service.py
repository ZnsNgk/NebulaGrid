"""小时用量物化：独立 bucket 保存已完成小时，页面与原始采样重建彻底分离。"""

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.services.metrics_service import escape_flux_string, query_flux, write_lines

RETENTION_DAYS = 90
RETENTION_SECONDS = RETENTION_DAYS * 86400
MEASUREMENT = "node_usage_hourly"
HOUR = timedelta(hours=1)


def hour_floor(value):
    """按部署时区的整点截断；查询 stop 为该整点，因此当前小时永不参与统计。"""
    return value.replace(minute=0, second=0, microsecond=0)


def retained_start(now):
    """向上对齐保留边界，避免已过期的半小时点被扫描器无限重建。"""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    floor = hour_floor(cutoff)
    return floor if floor == cutoff else floor + HOUR


def summary_settings(settings):
    if not settings.influxdb_usage_bucket or settings.influxdb_usage_bucket == settings.influxdb_bucket:
        raise ValueError("小时汇总 bucket 必须独立于原始监控 bucket")
    return replace(settings, influxdb_bucket=settings.influxdb_usage_bucket)


def influx_json(settings, path, body=None, method="GET"):
    """仅用于汇总 bucket 的初始化；不输出 token，不调整原始数据权限。"""
    request = urllib.request.Request(
        settings.influxdb_url.rstrip("/") + "/api/v2/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Token {settings.influxdb_token}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def ensure_usage_bucket(settings):
    """幂等创建或修正专用 bucket 的 90 天保留期；没有管理权限时交由运维预建。"""
    target = summary_settings(settings)
    query = urllib.parse.urlencode({"org": settings.influxdb_org, "name": target.influxdb_bucket})
    try:
        buckets = influx_json(settings, "buckets?" + query).get("buckets", [])
    except urllib.error.HTTPError as exc:
        # 首次按名称查找不存在的 bucket 时可能返回 404，而不是成功的空列表。
        # 仅此处允许继续创建；下方必须先读到原始 bucket，避免将错误地址/组织当成首次初始化。
        if exc.code != 404:
            raise
        exc.close()
        buckets = []
    bucket = next((b for b in buckets if b["name"] == target.influxdb_bucket), None)
    rules = [{"type": "expire", "everySeconds": RETENTION_SECONDS}]
    if bucket is None:
        # 从已配置的原始 bucket 取得 orgID，避免要求额外列举所有组织的权限。
        source_query = urllib.parse.urlencode({"org": settings.influxdb_org, "name": settings.influxdb_bucket})
        sources = influx_json(settings, "buckets?" + source_query).get("buckets", [])
        source = next((b for b in sources if b["name"] == settings.influxdb_bucket), None)
        if source is None:
            raise ValueError("无法读取原始监控 bucket，未创建小时汇总；请检查 InfluxDB 地址、组织、bucket 名和读取权限")
        influx_json(settings, "buckets", {"name": target.influxdb_bucket, "orgID": source["orgID"],
                    "description": "NebulaGrid 每节点每小时 CPU/GPU 均值及完成标记", "retentionRules": rules}, "POST")
    elif len(bucket.get("retentionRules", [])) != 1 or bucket["retentionRules"][0].get("everySeconds") != RETENTION_SECONDS:
        influx_json(settings, "buckets/" + urllib.parse.quote(bucket["id"], safe=""), {"retentionRules": rules}, "PATCH")


def node_filter(node_ids):
    return " or ".join(f'r.node_id == "{int(node_id)}"' for node_id in node_ids) or "false"


def read_hourly_rollups(settings, node_ids, start, stop):
    """至多读取 90 天的少量汇总点；不做原始数据回退，也不在读请求里创建 bucket。"""
    target = summary_settings(settings)
    stop = hour_floor(stop)
    if not node_ids or stop <= start:
        return {}
    flux = f'''
from(bucket: "{escape_flux_string(target.influxdb_bucket)}")
  |> range(start: time(v: "{start.isoformat()}"), stop: time(v: "{stop.isoformat()}"))
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and ({node_filter(node_ids)}) and
    (r._field == "cpu_usage" or r._field == "gpu_usage" or r._field == "complete"))
'''
    values = defaultdict(dict)
    allowed = set(node_ids)
    for row in query_flux(target, flux):
        try:
            node_id = int(row["node_id"])
            hour = datetime.fromisoformat(row["_time"].replace("Z", "+00:00")).astimezone(start.tzinfo)
            value = float(row["_value"])
        except (KeyError, ValueError, TypeError):
            continue
        field = row.get("_field")
        if node_id in allowed and start <= hour < stop and hour == hour_floor(hour) and math.isfinite(value):
            values[(node_id, hour)][field] = value
    # 只有两种均值和完成标记同时存在才算已完成；中断/部分写入会在后台重建。
    return {key: item for key, item in values.items() if item.get("complete") == 1
            and all(field in item and 0 <= item[field] <= 100 for field in ("cpu_usage", "gpu_usage"))}


def missing_hours(node_ids, start, stop, existing):
    """按小时检测缺口，倒序让最近数据先可用；已写入的零值同样视为完成。"""
    hour = hour_floor(stop) - HOUR
    result = []
    while hour >= start:
        missing = [node_id for node_id in node_ids if (node_id, hour) not in existing]
        if missing:
            result.append((hour, missing))
        hour -= HOUR
    return result


def rebuild_hour(settings, node_ids, hour, now):
    """每次最多扫一小时原始采样；成功空结果写零，任何查询/解析失败均不写完成标记。"""
    target = summary_settings(settings)
    if hour != hour_floor(hour) or hour < retained_start(now) or hour + HOUR > hour_floor(now):
        raise ValueError("仅允许重建保留期内已结束的完整小时")
    if not node_ids:
        return
    flux = f'''
from(bucket: "{escape_flux_string(settings.influxdb_bucket)}")
  |> range(start: time(v: "{hour.isoformat()}"), stop: time(v: "{(hour + HOUR).isoformat()}"))
  |> filter(fn: (r) => ({node_filter(node_ids)}) and (
    (r._measurement == "node_metrics" and r._field == "cpu_usage") or
    (r._measurement == "gpu_metrics" and r._field == "gpu_usage")))
  |> group(columns: ["_measurement", "node_id", "_field"])
  |> mean()
'''
    values = {node_id: {"cpu_usage": 0.0, "gpu_usage": 0.0} for node_id in node_ids}
    for row in query_flux(settings, flux, timeout_seconds=settings.influxdb_usage_timeout_seconds):
        node_id = int(row["node_id"])
        field = row["_field"]
        value = float(row["_value"])
        if node_id not in values or field not in {"cpu_usage", "gpu_usage"} or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("小时原始聚合结果异常，不写入零值完成记录")
        values[node_id][field] = value
    # 同节点、同时间戳重试会覆盖同一点；均值必须使用浮点字段，不能沿用实时写入的整数 i 后缀。
    timestamp = int(hour.astimezone(timezone.utc).timestamp()) * 1_000_000_000
    lines = [f'{MEASUREMENT},node_id={node_id} cpu_usage={item["cpu_usage"]},gpu_usage={item["gpu_usage"]},complete=1i {timestamp}'
             for node_id, item in values.items()]
    write_lines(target, lines)


def daily_from_rollups(node_ids, start, stop, hourly):
    """日均仅按已有的完整小时计算；缺失小时不占分母，并保留完整度供页面提示。"""
    stop = hour_floor(stop)
    result = {}
    pending = False
    day = start
    while day < stop:
        end = min(day + timedelta(days=1), stop)
        hours = []
        hour = day
        while hour < end:
            hours.append(hour)
            hour += HOUR
        for node_id in node_ids:
            samples = [hourly[(node_id, hour)] for hour in hours if (node_id, hour) in hourly]
            complete = len(samples) == len(hours)
            pending |= not complete
            result[(node_id, day.date().isoformat())] = {
                "hours": len(samples), "expected_hours": len(hours),
                # 服务重启可能留下日内缺口，不能因此丢弃其余 23 小时，也不能按 24 小时稀释均值。
                # 已保存的零值仍是有效汇总；只有完全没有有效小时的日期才显示无数据。
                "cpu_usage": sum(item["cpu_usage"] for item in samples) / len(samples) if samples else None,
                "gpu_usage": sum(item["gpu_usage"] for item in samples) / len(samples) if samples else None,
            }
        day += timedelta(days=1)
    return result, "building" if pending else "ok"
