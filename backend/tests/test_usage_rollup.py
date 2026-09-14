"""覆盖小时物化的持久完成标记、失败重试、保留边界与页面只读路径，不连接真实监控库。"""

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.config import Settings
from app.services import metrics_service, usage_rollup_service as rollup
from app.workers import usage_rollup as worker

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 9, 14, tzinfo=TZ)
SETTINGS = Settings(influxdb_token="test", influxdb_usage_timeout_seconds=90)


def rows_for(hour, node_id=1, cpu=30.5, gpu=60.5, complete=True):
    fields = {"cpu_usage": cpu, "gpu_usage": gpu}
    if complete:
        fields["complete"] = 1
    return [{"node_id": str(node_id), "_time": hour.astimezone(timezone.utc).isoformat(),
             "_field": field, "_value": str(value), "_measurement": rollup.MEASUREMENT}
            for field, value in fields.items()]


def test_bucket_create_retention_and_existing_are_idempotent(monkeypatch):
    calls = []
    stored = []
    def api(settings, path, body=None, method="GET"):
        calls.append((path, body, method))
        if method == "POST":
            stored.append(dict(body, id="summary-id"))
            return stored[0]
        if "name=nebulagrid_metrics" in path:
            return {"buckets": [{"name": settings.influxdb_bucket, "orgID": "org-id"}]}
        return {"buckets": stored}
    monkeypatch.setattr(rollup, "influx_json", api)
    rollup.ensure_usage_bucket(SETTINGS)
    rollup.ensure_usage_bucket(SETTINGS)
    writes = [(path, body) for path, body, method in calls if method != "GET"]
    assert len(writes) == 1
    assert writes[0][1]["name"] == "nebulagrid_usage_hourly"
    assert writes[0][1]["orgID"] == "org-id"
    assert writes[0][1]["retentionRules"] == [{"type": "expire", "everySeconds": 7776000}]


def test_bucket_corrects_only_summary_retention_and_rejects_raw_bucket(monkeypatch):
    calls = []
    def api(settings, path, body=None, method="GET"):
        calls.append((path, body, method))
        return {"buckets": [{"id": "summary-id", "name": settings.influxdb_usage_bucket, "retentionRules": []}]}
    monkeypatch.setattr(rollup, "influx_json", api)
    rollup.ensure_usage_bucket(SETTINGS)
    assert calls[-1] == ("buckets/summary-id", {"retentionRules": [{"type": "expire", "everySeconds": 7776000}]}, "PATCH")
    with pytest.raises(ValueError):
        rollup.ensure_usage_bucket(Settings(influxdb_usage_bucket="nebulagrid_metrics"))
    assert len(calls) == 2


def test_missing_bucket_http_404_creates_then_restart_reuses_it(monkeypatch):
    """复现线上首次部署：经过实际 URL/HTTP/JSON 路径，按名称查找返回 404 仍能创建。"""
    requests = []
    stored = []
    def http(request, timeout):
        path = urlsplit(request.full_url)
        requests.append((request.get_method(), path.path, parse_qs(path.query)))
        assert timeout == 10
        if request.get_method() == "POST":
            bucket = json.loads(request.data)
            assert bucket["orgID"] == "org-id"
            assert bucket["name"] == SETTINGS.influxdb_usage_bucket
            assert bucket["retentionRules"] == [{"type": "expire", "everySeconds": 7776000}]
            stored.append(dict(bucket, id="summary-id"))
            return BytesIO(json.dumps(stored[0]).encode())
        name = parse_qs(path.query)["name"][0]
        if name == SETTINGS.influxdb_bucket:
            result = {"buckets": [{"name": name, "orgID": "org-id"}]}
        elif not stored:
            raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(b'{"code":"not found","message":"bucket not found"}'))
        else:
            result = {"buckets": stored}
        return BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(rollup.urllib.request, "urlopen", http)
    rollup.ensure_usage_bucket(SETTINGS)
    rollup.ensure_usage_bucket(SETTINGS)
    assert [method for method, _, _ in requests] == ["GET", "GET", "POST", "GET"]
    assert all(path == "/api/v2/buckets" for _, path, _ in requests)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_lookup_http_errors_do_not_trigger_creation(monkeypatch, status):
    calls = []
    def http(request, timeout):
        calls.append(request.get_method())
        raise HTTPError(request.full_url, status, "failed", {}, BytesIO())
    monkeypatch.setattr(rollup.urllib.request, "urlopen", http)
    with pytest.raises(HTTPError) as caught:
        rollup.ensure_usage_bucket(SETTINGS)
    assert caught.value.code == status and calls == ["GET"]


@pytest.mark.parametrize("source_status", [200, 401, 403, 404, 500])
def test_target_404_requires_readable_source_before_creation(monkeypatch, source_status):
    """错误端点或组织也可能返回 404；没有可读原始 bucket 时不得擅自创建任何资源。"""
    calls = []
    def http(request, timeout):
        calls.append(request.get_method())
        assert request.get_method() == "GET"
        if len(calls) == 2 and source_status == 200:
            return BytesIO(b'{"buckets":[]}')
        status = 404 if len(calls) == 1 else source_status
        raise HTTPError(request.full_url, status, "failed", {}, BytesIO())
    monkeypatch.setattr(rollup.urllib.request, "urlopen", http)
    with pytest.raises(ValueError if source_status == 200 else HTTPError):
        rollup.ensure_usage_bucket(SETTINGS)
    assert calls == ["GET", "GET"]


def test_hourly_page_reads_only_rollup_bucket_with_short_timeout(monkeypatch):
    """真实 HTTP 构造和 CSV 解析：零值参与均值，当前小时和越权节点被排除。"""
    samples = rows_for(START) + rows_for(START + rollup.HOUR, cpu=0, gpu=0)
    samples += rows_for(START + 2 * rollup.HOUR, cpu=100, gpu=100) + rows_for(START, node_id=2)
    fields = ["_measurement", "node_id", "_time", "_field", "_value"]
    csv = ",result,table," + ",".join(fields) + "\n"
    csv += "\n".join(",_result,0," + ",".join(row[field] for field in fields) for row in samples)
    requests = []
    def http(request, timeout):
        requests.append(json.loads(request.data)["query"])
        assert timeout == 5
        return BytesIO(csv.encode())
    monkeypatch.setattr(metrics_service.urllib.request, "urlopen", http)
    monkeypatch.setattr(metrics_service, "get_settings", lambda: SETTINGS)
    result, status = metrics_service.get_daily_usage_metrics([1], START, START + timedelta(hours=2, minutes=25))
    assert status == "ok"
    assert result == {(1, "2026-09-14"): {"cpu_usage": 15.25, "gpu_usage": 30.25, "hours": 2, "expected_hours": 2}}
    assert 'from(bucket: "nebulagrid_usage_hourly")' in requests[0]
    assert 'stop: time(v: "2026-09-14T02:00:00+08:00")' in requests[0]
    assert "node_metrics" not in requests[0] and "mean(" not in requests[0]


def test_partial_invalid_and_missing_hours_are_rebuilt_newest_first(monkeypatch):
    rows = rows_for(START, cpu=0, gpu=0)
    rows += rows_for(START + rollup.HOUR, complete=False)
    rows += rows_for(START + 2 * rollup.HOUR, cpu=float("nan"))
    rows += rows_for(START + timedelta(minutes=30))
    monkeypatch.setattr(rollup, "query_flux", lambda *args: rows)
    existing = rollup.read_hourly_rollups(SETTINGS, [1], START, START + 3 * rollup.HOUR)
    assert list(existing) == [(1, START)]
    assert rollup.missing_hours([1, 2], START, START + 3 * rollup.HOUR, existing) == [
        (START + 2 * rollup.HOUR, [1, 2]), (START + rollup.HOUR, [1, 2]), (START, [2])]
    result, status = rollup.daily_from_rollups([1], START, START + 3 * rollup.HOUR, existing)
    assert status == "building"
    assert result[(1, "2026-09-14")] == {"hours": 1, "expected_hours": 3, "cpu_usage": 0, "gpu_usage": 0}


def test_restart_gap_keeps_other_23_hours_and_excludes_missing_hour_from_mean():
    """重现截图 23 / 24：服务重启缺一小时，其余小时均值仍可显示，分母不得补成 24。"""
    hourly = {(1, START + index * rollup.HOUR): {"cpu_usage": 46, "gpu_usage": 69}
              for index in range(24) if index != 7}
    result, status = rollup.daily_from_rollups([1, 2], START, START + timedelta(days=1), hourly)
    assert status == "building"
    assert result[(1, "2026-09-14")] == {"hours": 23, "expected_hours": 24, "cpu_usage": 46, "gpu_usage": 69}
    assert result[(2, "2026-09-14")] == {"hours": 0, "expected_hours": 24, "cpu_usage": None, "gpu_usage": None}
    # 后台若补回缺口，下一次读取自然使用新增小时，无需重写已有汇总点。
    hourly[(1, START + 7 * rollup.HOUR)] = {"cpu_usage": 22, "gpu_usage": 21}
    result, status = rollup.daily_from_rollups([1], START, START + timedelta(days=1), hourly)
    assert status == "ok"
    assert result[(1, "2026-09-14")] == {"hours": 24, "expected_hours": 24, "cpu_usage": 45, "gpu_usage": 67}


def test_rebuild_writes_mean_and_offline_zero_with_fixed_hour_timestamp(monkeypatch):
    writes = []
    queries = []
    def query(settings, flux, timeout_seconds):
        assert settings.influxdb_bucket == SETTINGS.influxdb_bucket and timeout_seconds == 90
        queries.append(flux)
        return [{"node_id": "1", "_field": "cpu_usage", "_value": "12.75"},
                {"node_id": "1", "_field": "gpu_usage", "_value": "42.5"}]
    monkeypatch.setattr(rollup, "query_flux", query)
    monkeypatch.setattr(rollup, "write_lines", lambda settings, lines: writes.append((settings.influxdb_bucket, lines)))
    for _ in range(2):
        rollup.rebuild_hour(SETTINGS, [1, 2], START, START + 2 * rollup.HOUR)
    timestamp = int(START.timestamp()) * 1_000_000_000
    assert writes[0] == ("nebulagrid_usage_hourly", [
        f"node_usage_hourly,node_id=1 cpu_usage=12.75,gpu_usage=42.5,complete=1i {timestamp}",
        f"node_usage_hourly,node_id=2 cpu_usage=0.0,gpu_usage=0.0,complete=1i {timestamp}"])
    assert writes[0] == writes[1]
    assert '|> mean()' in queries[0]
    assert 'range(start: time(v: "2026-09-14T00:00:00+08:00"), stop: time(v: "2026-09-14T01:00:00+08:00"))' in queries[0]


def test_empty_query_is_completed_and_survives_restart_scan(monkeypatch):
    """模拟写入后下次扫描：整小时离线写零，无须反复查询原始数据。"""
    lines = []
    monkeypatch.setattr(rollup, "query_flux", lambda *args, **kwargs: [])
    monkeypatch.setattr(rollup, "write_lines", lambda settings, data: lines.extend(data))
    rollup.rebuild_hour(SETTINGS, [1], START, START + rollup.HOUR)
    assert "cpu_usage=0.0,gpu_usage=0.0,complete=1i" in lines[0]
    monkeypatch.setattr(rollup, "query_flux", lambda *args: rows_for(START, cpu=0, gpu=0))
    existing = rollup.read_hourly_rollups(SETTINGS, [1], START, START + rollup.HOUR)
    assert rollup.missing_hours([1], START, START + rollup.HOUR, existing) == []


@pytest.mark.parametrize("failure", [TimeoutError(), ValueError("bad csv"), OSError("offline")])
def test_failed_raw_query_never_marks_hour_complete(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(rollup, "query_flux", fail)
    monkeypatch.setattr(rollup, "write_lines", lambda *args: pytest.fail("查询失败不得写零"))
    with pytest.raises(type(failure)):
        rollup.rebuild_hour(SETTINGS, [1], START, START + rollup.HOUR)


@pytest.mark.parametrize("csv", [",error,reference\n,query timeout,123", ",error,reference\n"])
def test_http_200_flux_error_is_not_empty_success(monkeypatch, csv):
    monkeypatch.setattr(metrics_service.urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(csv.encode()))
    monkeypatch.setattr(rollup, "write_lines", lambda *args: pytest.fail("错误表不得持久化为零"))
    with pytest.raises(ValueError, match="error table"):
        rollup.rebuild_hour(SETTINGS, [1], START, START + rollup.HOUR)


@pytest.mark.parametrize("value", ["NaN", "inf", "-1", "101", "bad"])
def test_invalid_mean_never_completes(monkeypatch, value):
    monkeypatch.setattr(rollup, "query_flux", lambda *args, **kwargs: [{"node_id": "1", "_field": "cpu_usage", "_value": value}])
    monkeypatch.setattr(rollup, "write_lines", lambda *args: pytest.fail("无效均值不得写入"))
    with pytest.raises(ValueError):
        rollup.rebuild_hour(SETTINGS, [1], START, START + rollup.HOUR)


@pytest.mark.parametrize("hour", [START + rollup.HOUR, START + timedelta(minutes=1), START - timedelta(days=90)])
def test_current_hour_and_expired_points_never_read_raw(monkeypatch, hour):
    monkeypatch.setattr(rollup, "query_flux", lambda *args, **kwargs: pytest.fail("禁止查询当前小时或过期点"))
    with pytest.raises(ValueError):
        rollup.rebuild_hour(SETTINGS, [1], hour, START + timedelta(hours=1, minutes=25))


def test_retention_rounds_up_and_midnight_has_no_current_day_sample():
    assert rollup.retained_start(START) == START - timedelta(days=90)
    assert rollup.retained_start(START + timedelta(minutes=1)) == START - timedelta(days=90) + rollup.HOUR
    assert rollup.daily_from_rollups([1], START, START + timedelta(minutes=25), {}) == ({}, "ok")


def test_worker_startup_scan_retries_failure_without_blocking_other_hours(monkeypatch):
    """两小时缺口中最新一小时失败，后续仍处理旧小时；不在循环中重试打满 InfluxDB。"""
    class Stop:
        count = 0
        def is_set(self):
            return self.count >= 3
        def wait(self, delay):
            self.count += 1
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def scalars(self, query):
            return [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    calls = []
    monkeypatch.setattr(worker, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(worker, "ensure_usage_bucket", lambda settings: calls.append("ensure"))
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker, "is_control_plane_node", lambda node: node.id == 2)
    monkeypatch.setattr(worker, "local_datetime", lambda: START + 2 * rollup.HOUR)
    monkeypatch.setattr(worker, "retained_start", lambda now: START)
    monkeypatch.setattr(worker, "read_hourly_rollups", lambda *args: {})
    def rebuild(settings, ids, hour, now):
        assert ids == [1]
        calls.append(hour)
        if hour == START + rollup.HOUR:
            raise TimeoutError()
    monkeypatch.setattr(worker, "rebuild_hour", rebuild)
    worker.rollup_loop(Stop(), SimpleNamespace(execute=lambda *args: None, commit=lambda: None))
    assert calls == ["ensure", START + rollup.HOUR, START]


def test_unconfigured_worker_does_not_start(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: Settings(influxdb_token=""))
    monkeypatch.setattr(worker.multiprocessing, "get_context", lambda *args: pytest.fail("未配置时不启动进程"))
    worker.start_usage_worker(SimpleNamespace(state=SimpleNamespace()))


def test_worker_rollover_prioritizes_new_hour_over_old_backfill(monkeypatch):
    class Stop:
        count = 0
        def is_set(self):
            return self.count >= 2
        def wait(self, delay):
            self.count += 1
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def scalars(self, query):
            return [SimpleNamespace(id=1)]
    stop = Stop()
    saved = {}
    rebuilt = []
    monkeypatch.setattr(worker, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(worker, "ensure_usage_bucket", lambda *args: None)
    monkeypatch.setattr(worker, "SessionLocal", Session)
    monkeypatch.setattr(worker, "is_control_plane_node", lambda node: False)
    # 第一次扫描还有历史积压，处理一个小时后跨过整点。
    monkeypatch.setattr(worker, "local_datetime", lambda: START + timedelta(hours=2 + stop.count, minutes=59))
    monkeypatch.setattr(worker, "retained_start", lambda now: START)
    monkeypatch.setattr(worker, "read_hourly_rollups", lambda *args: saved)
    def rebuild(settings, ids, hour, now):
        saved[(1, hour)] = {"cpu_usage": 0, "gpu_usage": 0, "complete": 1}
        rebuilt.append(hour)
    monkeypatch.setattr(worker, "rebuild_hour", rebuild)
    worker.rollup_loop(stop, SimpleNamespace(execute=lambda *args: None, commit=lambda: None))
    assert rebuilt == [START + rollup.HOUR, START + 2 * rollup.HOUR]


@pytest.mark.parametrize("locked", [True, False])
def test_worker_only_aggregates_while_holding_separate_session_lock(monkeypatch, locked):
    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, delay):
            self.stopped = True
    calls = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def scalar(self, query):
            calls.append(str(query))
            return locked
        def execute(self, query):
            calls.append(str(query))
        def commit(self):
            pass
    monkeypatch.setattr(worker, "engine", SimpleNamespace(connect=Connection))
    monkeypatch.setattr(worker, "rollup_loop", lambda *args: calls.append("aggregate"))
    worker.run_usage_worker(Stop())
    assert ("aggregate" in calls) == locked
    assert "pg_try_advisory_lock(731290462)" in calls[0]
    if locked:
        assert "pg_advisory_unlock(731290462)" in calls[-1]


def _usage_process_probe(stop):
    """真实子进程只写测试标记，验证进程隔离和停止，不访问生产数据库。"""
    import os
    from pathlib import Path
    marker = Path(os.environ["NEBULAGRID_TEST_USAGE_PROBE"])
    marker.write_text(str(os.getpid()))
    stop.wait(15)
    marker.write_text("stopped")


def test_usage_process_is_isolated_and_stops_with_api(tmp_path, monkeypatch):
    import os
    import time
    marker = tmp_path / "usage-probe"
    monkeypatch.setenv("NEBULAGRID_TEST_USAGE_PROBE", str(marker))
    monkeypatch.setattr(worker, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(worker, "run_usage_worker", _usage_process_probe)
    app = SimpleNamespace(state=SimpleNamespace())
    worker.start_usage_worker(app)
    thread = app.state.usage_rollup_thread
    worker.start_usage_worker(app)
    assert app.state.usage_rollup_thread is thread
    try:
        deadline = time.monotonic() + 10
        value = ""
        while time.monotonic() < deadline:
            value = marker.read_text() if marker.exists() else ""
            if value.isdigit():
                break
            time.sleep(0.02)
        assert value.isdigit() and int(value) != os.getpid()
    finally:
        worker.stop_usage_worker(app)
    assert not thread.is_alive()
    assert marker.read_text() == "stopped"
