"""使用真实日志格式的精简片段与隔离 SQLite，禁止连接运行中的生产集群。"""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.time_utils import local_datetime
from app.db.base import Base
from app.db.models import Setting, Task, TaskAllocation, TaskProgress, TaskRequirement, User
from app.services import task_progress_service as service
from app.services.log_progress import LogProgressParser, seconds
from app.services.task_timing import backfill_task_durations, fill_missing_finished_durations


def parse(text, now=1000):
    parser = LogProgressParser()
    parser.feed(text.encode())
    parser.observe(now)
    return parser


@pytest.mark.parametrize("unit", ["Epoch", "Episode", "Task", "Trial", "Round", "Fold"])
def test_keyword_units(unit):
    p = parse(f"{unit} 1/100\n")
    assert f"{unit} 1/100" in p.summary(1000)["text"]
    p.feed(f"{unit} 2/100\n".encode())
    p.observe(1060)
    assert p.summary(1060)["remaining_seconds"] == 99 * 60


def test_config_override_and_non_total_keywords():
    p = parse("MAX_ITER: 40000\nMAX_ITER: 25000\nSTEPS: (25000,)\n"
              "Training Epoch: 1000\nTrained Model Save Step: 10\n"
              "warmup_epochs=20, warmup_steps=5000, start_epoch=0\n"
              "EarlyStopping counter: 3 out of 30\n")
    assert p.state["totals"] == {"step": 25000, "epoch": 1000}
    assert not p.state["outer"]


def test_metric_epoch_validation_ema_cycle():
    p = parse("Namespace(epochs=300, warmup_epochs=20)\n"
              "Epoch: [0] [0/10009] eta: 3 days, 21:30:49\n"
              "Epoch: [0] Total time: 0:28:15\n"
              "Test: Total time: 0:01:15\nTest: Total time: 0:00:46\n"
              "Epoch: [1] [0/10009] eta: 0:28:00\n")
    assert p.state["cycles"] == [1816]
    assert p.summary(1000)["scope"] == "task"
    assert p.summary(1000)["remaining_seconds"] == 299 * 1816
    assert p.state["outer"][0]["base"] == 0


def test_lightning_cycle_does_not_count_validation_twice():
    p = parse("max_epochs=10\n"
              "Sanity Checking DataLoader 0: 100%|xx| 2/2 [00:01<00:00, 2.00it/s]\r"
              "Epoch 0: 100%|xx| 100/100 [02:00<00:00, 1.20s/it]\r"
              "Validation DataLoader 0: 100%|xx| 20/20 [01:00<00:00, 3.00s/it]\r"
              "Epoch 0: 100%|xx| 100/100 [03:00<00:00, 1.80s/it]\r"
              "Epoch 0: 0%|xx| 0/100 [00:00<?, ?it/s]\r"
              "Epoch 1: 0%|xx| 0/100 [00:00<?, ?it/s]\r")
    assert p.state["cycles"] == [180]
    assert p.summary(1000)["remaining_seconds"] == 9 * 180


def test_tqdm_partial_utf8_ansi_and_postfix_duplicates():
    text = "Epoch [1/300]: 15%|██| 356/2398 [2:22:46<7:46:02, 13.69s/it, loss=3.2]"
    data = ("\r\x1b[32m" + text + "\x1b[0m").encode()
    p = LogProgressParser()
    for start in range(0, len(data), 7):
        p.feed(data[start:start+7])
    p.observe(1000)
    first = p.summary(1000)
    assert "Epoch 1/300" in first["text"]
    assert "Step 356/2398" in first["text"]
    assert first["remaining_seconds"] == round(((2398-356) + 299*2398)*13.69)
    assert first["scope"] == "task"
    assert first["estimate_kind"] == "rough"
    p.feed(("\r" + text.replace("loss=3.2", "loss=4.0") + "\r").encode())
    p.observe(1060)
    assert len(p.state["inner"]["rates"]) == 1
    assert p.summary(1300)["remaining_seconds"] is None
    assert p.summary(1300)["stale"]


def test_unlabelled_inner_bar_does_not_replace_whole_task_eta():
    p = parse("Training Epoch: 1000\n"
              "Epoch 1: 100%|x| 1000/1000 [03:00<00:00, 5.55it/s]\r"
              "Epoch 1: 50%|x| 5/10 [00:05<00:05, 1.00it/s]\r")
    assert p.summary(1000)["scope"] == "stage"
    assert p.summary(1000)["remaining_seconds"] == 5


def test_restart_marker_clears_config_and_samples():
    p = parse("epochs=300\nEpoch: [100] [10/20] eta: 00:10\n"
              "[NebulaGrid] task started at 2026-09-06T00:00:00+08:00\n"
              "Episode 1/8\n")
    assert p.state["totals"] == {}
    assert p.summary(1000)["text"] == "Episode 1/8"


def test_trial_rate_and_unknown_initial_rate():
    p = parse("0%|x| 0/30 [00:00<?, ?trial/s]\r")
    assert p.summary(1000)["remaining_seconds"] is None
    p.feed(b"50%|x| 15/30 [30:00<30:00, 120.00s/trial]\r")
    p.observe(1060)
    assert "Trial 15/30" in p.summary(1060)["text"]
    assert p.summary(1060)["remaining_seconds"] == 1800


def test_seed_task_cycle_and_duplicate_rank_logs():
    p = parse("[seed 42] python train.py --epochs 50\n"
              "Task 1/23: nodes=100\nTask 1 done: Task-IL=0.9, time=100.0s\n"
              "Task 1 done: Task-IL=0.9, time=100.0s\nTask 2/23: nodes=200\n"
              "[rank1] Task 9/23: nodes=900\n")
    assert "Seed 42 · Task 2/23" in p.summary(1000)["text"]
    assert p.summary(1000)["scope"] == "stage"
    assert p.summary(1000)["remaining_seconds"] == 2200
    p.feed(b"[seed 43] python train.py --epochs 50\nTask 1/23: nodes=100\n")
    p.observe(1060)
    assert p.summary(1060)["remaining_seconds"] is None


def test_live_step_speed_ignores_replayed_records():
    p = parse("Step 1/1000\nStep 200/1000\n")
    assert p.summary(1000)["remaining_seconds"] is None
    p.feed(b"Step 260/1000\n")
    p.observe(1060)
    assert p.summary(1060)["remaining_seconds"] == 740
    assert seconds("90:31:54") == 325914
    assert seconds("1 day, 4:03:20") == 101000


def test_real_moe_metrics_do_not_freeze_epoch_after_validation():
    # 来自运行任务 260905214745358 的格式；不保留业务命令和数据集信息。
    p = parse("epochs=300\nNumber of training training per epoch = 5004\n"
              "Epoch: [0] [0/2502] eta: 00:10 steps: 12.0000 (12.0000)\n"
              "Epoch: [15] Total time: 0:25:19\nTest: [65/66] eta: 00:00\n"
              "Test: Total time: 00:56\n"
              "Epoch: [16] [60/2502] eta: 0:25:24 steps: 11.9766 (11.9524) executed_steps: 12\n")
    assert p.state["totals"] == {"epoch": 300}
    assert p.state["outer"][0]["base"] == 0
    assert p.summary(1000)["text"] == "训练 · Epoch 16/300 · Step 60/2502"
    assert p.summary(1000)["remaining_seconds"] == 283 * 1575 + 1524 + 56
    assert p.summary(1000)["scope"] == "task"


@pytest.mark.parametrize("metric", ["steps: 12", "steps: 11.97", "total_steps: 12.0"])
def test_runtime_metrics_do_not_override_total_steps(metric):
    p = parse(f"max_steps=5000\nEpoch: [0] [10/100] eta: 00:90 {metric}\n")
    assert p.state["totals"] == {"step": 5000}
    assert p.state["inner"]["current"] == 10


def test_per_epoch_metadata_is_not_a_current_epoch():
    p = parse("epochs=300\nNumber of training training per epoch = 5004\n")
    assert p.state["outer"] == []
    p.feed(b"Epoch: [0] [0/10009] eta: 01:00\n")
    assert p.state["outer"][0]["base"] == 0


def test_whole_task_eta_includes_remaining_validation_and_future_cycles():
    p = parse("epochs=3\nEpoch: [0] [0/100] eta: 02:00\n"
              "Epoch: [0] Total time: 02:00\nTest: Total time: 00:30\n"
              "Epoch: [1] [50/100] eta: 01:00\n")
    assert p.summary(1000)["scope"] == "task"
    assert p.summary(1000)["remaining_seconds"] == 60 + 30 + 150


def test_first_epoch_can_extrapolate_with_explicit_rough_label():
    p = parse("Epoch 1/10: [25/100] eta: 01:15\n")
    result = p.summary(1000)
    assert result["scope"] == "task"
    assert result["remaining_seconds"] == 75 + 9 * 100
    assert result["estimate_kind"] == "rough"
    assert "尚未计入验证" in result["reason"]


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(task_log_root=str(tmp_path)))
    with factory() as db:
        db.add(User(id=1, username="admin", real_name="admin", role="admin", password_hash="x", home_path="/"))
        db.add(Setting(key=service.INTERVAL_KEY, value="60"))
        db.commit()
    yield factory
    engine.dispose()


def create_task(factory, tmp_path, state="running", number=1):
    path = tmp_path / f"{number}.log"
    path.write_text("epochs=20\nEpoch: [0] [10/100] eta: 01:30\n", encoding="utf-8")
    with factory() as db:
        task = Task(task_id=f"test-{number}", user_id=1, workdir="/", command="python train.py",
                    state=state, log_path=str(path), started_at=local_datetime())
        db.add(task)
        db.flush()
        db.add(TaskRequirement(task_id=task.id, need_gpus=0))
        db.commit()
        return task.id, path


def test_only_running_logs_and_interval_hot_reload(database, tmp_path, monkeypatch):
    ids = [create_task(database, tmp_path, s, i+1)[0] for i, s in enumerate(
        ["running", "wait", "succeeded", "starting", "cancelling"])]
    monkeypatch.setattr(service.time, "time", lambda: 1000)
    service.progress_tick(database)
    with database() as db:
        rows = db.scalars(select(TaskProgress)).all()
        assert [row.task_id for row in rows] == [ids[0]]
        assert rows[0].summary["text"] == "训练 · Epoch 0/20 · Step 10/100"
        initial = rows[0].version
        db.get(Setting, service.INTERVAL_KEY).value = "10"
        db.commit()
    monkeypatch.setattr(service.time, "time", lambda: 1011)
    service.progress_tick(database)
    with database() as db:
        assert db.get(TaskProgress, ids[0]).version > initial


def test_upgrade_catchup_survives_restart_and_keeps_header(database, tmp_path, monkeypatch):
    task_id, path = create_task(database, tmp_path)
    path.write_text("epochs=300\n" + "noise\n" * 50 + "Epoch: [200] [5/10] eta: 00:05\n")
    monkeypatch.setattr(service, "READ_LIMIT", 80)
    for i in range(10):
        monkeypatch.setattr(service.time, "time", lambda i=i: 1000 + i * 6)
        service.scan_task(task_id, 60, database)
    with database() as db:
        row = db.get(TaskProgress, task_id)
        assert row.parser_state["offset"] == path.stat().st_size
        assert not row.parser_state["catching_up"]
        assert "Epoch 200/300" in row.summary["text"]
        assert row.summary["scope"] == "stage"


def test_read_chunk_setting_hot_reload_preserves_cursor(database, tmp_path, monkeypatch):
    task_id, path = create_task(database, tmp_path)
    data = b"epochs=20\n" + (b"." * 1000 + b"\n") * 2200 + b"Epoch: [0] [10/100] eta: 01:30\n"
    path.write_bytes(data)
    with database() as db:
        db.add(Setting(key=service.READ_CHUNK_KEY, value="1"))
        db.commit()
    monkeypatch.setattr(service.time, "time", lambda: 1000)
    service.progress_tick(database)
    with database() as db:
        row = db.get(TaskProgress, task_id)
        key = row.run_key
        assert row.parser_state["offset"] == 1024 * 1024
        assert row.summary["catchup"]["read_bytes"] == 1024 * 1024
        assert row.summary["catchup"]["total_bytes"] == len(data)
        db.get(Setting, service.READ_CHUNK_KEY).value = "4"
        db.commit()
    monkeypatch.setattr(service.time, "time", lambda: 1006)
    service.progress_tick(database)
    with database() as db:
        row = db.get(TaskProgress, task_id)
        assert row.run_key == key
        assert row.parser_state["offset"] == len(data)
        assert "catchup" not in row.summary
        assert "Epoch 0/20" in row.summary["text"]


def test_catchup_progress_notifies_in_five_percent_steps(database, tmp_path, monkeypatch):
    task_id, path = create_task(database, tmp_path)
    path.write_bytes(b"noise\n" * 2000)
    monkeypatch.setattr(service, "READ_LIMIT", 120)
    versions = []
    for i in range(6):
        monkeypatch.setattr(service.time, "time", lambda i=i: 1000 + i * 6)
        service.scan_task(task_id, 60, database)
        with database() as db:
            row = db.get(TaskProgress, task_id)
            assert row.summary["catchup"]["percent"] == i + 1
            versions.append(row.summary_version)
    assert versions[0] == versions[3]
    assert versions[4] > versions[3]
    assert versions[5] == versions[4]


@pytest.mark.parametrize("value", [0, 17, -1, "4.5", "wrong", True])
def test_read_chunk_setting_rejects_invalid_values(value):
    from app.core.errors import AppError
    from app.services.audit_service import normalize_setting_value
    with pytest.raises(AppError):
        normalize_setting_value(service.READ_CHUNK_KEY, value)


def test_reexecution_and_rotation_do_not_reuse_old_progress(database, tmp_path, monkeypatch):
    task_id, path = create_task(database, tmp_path)
    monkeypatch.setattr(service.time, "time", lambda: 1000)
    service.scan_task(task_id, 60, database)
    path.write_text("Episode 1/5\n")
    with database() as db:
        task = db.get(Task, task_id)
        task.started_at += timedelta(hours=1)
        db.commit()
    service.scan_task(task_id, 60, database)
    with database() as db:
        assert db.get(TaskProgress, task_id).summary["text"] == "Episode 1/5"
    path.write_text("Episode 2/9\n")
    monkeypatch.setattr(service.time, "time", lambda: 1061)
    service.scan_task(task_id, 60, database)
    with database() as db:
        assert db.get(TaskProgress, task_id).summary["text"] == "Episode 2/9"


def test_parser_upgrade_rebuilds_old_polluted_state(database, tmp_path, monkeypatch):
    task_id, _ = create_task(database, tmp_path)
    monkeypatch.setattr(service.time, "time", lambda: 1000)
    service.scan_task(task_id, 60, database)
    with database() as db:
        row = db.get(TaskProgress, task_id)
        row.parser_state = {"offset": 9999, "parser": {"totals": {"step": 12}}}
        previous_key = row.run_key
        db.commit()
    monkeypatch.setattr(service, "PARSER_VERSION", service.PARSER_VERSION + 1)
    service.progress_tick(database)
    with database() as db:
        row = db.get(TaskProgress, task_id)
        assert row.run_key != previous_key
        assert row.parser_state["parser"]["totals"] == {"epoch": 20}
        assert "Epoch 0/20" in row.summary["text"]


def test_finishes_during_scan_discards_result(database, tmp_path, monkeypatch):
    task_id, path = create_task(database, tmp_path)
    read = service.read_log
    def finish(*args):
        with database() as db:
            task = db.get(Task, task_id)
            task.state = "succeeded"
            task.finished_at = task.started_at + timedelta(seconds=90)
            db.commit()
        return read(*args)
    monkeypatch.setattr(service, "read_log", finish)
    service.scan_task(task_id, 60, database)
    with database() as db:
        assert db.get(TaskProgress, task_id).summary == {}
        assert db.get(Task, task_id).duration_seconds == 90


def test_missing_log_and_lease_expiry_retry(database, tmp_path, monkeypatch):
    task_id, path = create_task(database, tmp_path)
    with database() as db:
        db.get(Task, task_id).log_path = str(tmp_path / "later.log")
        db.add(TaskProgress(task_id=task_id, lease_until=1100, version=1))
        db.commit()
    monkeypatch.setattr(service.time, "time", lambda: 1000)
    service.scan_task(task_id, 60, database)
    with database() as db:
        assert db.get(TaskProgress, task_id).version == 1
    monkeypatch.setattr(service.time, "time", lambda: 1101)
    service.scan_task(task_id, 60, database)
    with database() as db:
        assert db.get(TaskProgress, task_id).summary["text"] == "日志暂不可读"
    (tmp_path / "later.log").write_text("Episode 1/5\n")
    monkeypatch.setattr(service.time, "time", lambda: 1162)
    service.scan_task(task_id, 60, database)
    with database() as db:
        assert db.get(TaskProgress, task_id).summary["text"] == "Episode 1/5"


def test_log_outside_configured_root_is_not_read(database, tmp_path):
    with pytest.raises(ValueError):
        service.read_log(str(tmp_path.parent / "outside.log"), {}, 1000, 60)


def test_duration_backfill_idempotent_and_unknown(database, tmp_path):
    task_id, _ = create_task(database, tmp_path, "succeeded")
    other, _ = create_task(database, tmp_path, "cancelled", 2)
    with database() as db:
        # 模拟老版本已落库的数据，直接 SQL 绕过新版本 ORM 的自动耗时计算。
        task = db.get(Task, task_id)
        db.execute(update(Task).where(Task.id == task_id).values(
            finished_at=task.started_at + timedelta(seconds=125), duration_seconds=None))
        db.commit()
    backfill_task_durations(database)
    backfill_task_durations(database)
    with database() as db:
        task = db.get(Task, task_id)
        assert task.duration_seconds == 125
        assert db.get(Task, other).duration_seconds is None
        assert db.get(Setting, "internal.task_duration_backfill_v1").value == "done"
        task.state = "wait"
        db.commit()
        assert task.duration_seconds is None


def test_old_executor_finishes_after_initial_upgrade_backfill(database, tmp_path):
    task_id, _ = create_task(database, tmp_path)
    backfill_task_durations(database)
    with database() as db:
        task = db.get(Task, task_id)
        db.execute(update(Task).where(Task.id == task_id).values(
            state="succeeded", finished_at=task.started_at + timedelta(seconds=180)))
        db.commit()
    fill_missing_finished_durations(database)
    with database() as db:
        assert db.get(Task, task_id).duration_seconds == 180


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "NaN", "3601", "abc"])
def test_invalid_admin_interval_rejected(value):
    from app.core.errors import AppError
    from app.services.audit_service import normalize_setting_value
    with pytest.raises(AppError):
        normalize_setting_value(service.INTERVAL_KEY, value)


def test_task_api_summary_permissions_and_sse(database, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.core.rbac import Role
    from app.services.auth_service import UserRecord
    from app.services.task_service import task_change_cursor
    from app.services import audit_service

    task_id, _ = create_task(database, tmp_path)
    history, _ = create_task(database, tmp_path, "succeeded", 2)
    user = UserRecord(1, "admin", "admin", Role.ADMIN, "enabled", "x")
    with database() as db:
        before = task_change_cursor(user, db)
    service.progress_tick(database)
    with database() as db:
        assert task_change_cursor(user, db) != before
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    def session():
        with database() as db:
            yield db
    app.dependency_overrides[get_db] = session
    monkeypatch.setattr(audit_service, "SessionLocal", database)
    client = TestClient(app)
    data = client.get("/api/tasks?state=running").json()["data"]["items"]
    assert data[0]["progress"]["text"].startswith("训练")
    assert "parser_state" not in data[0]
    assert client.get("/api/tasks?state=history").json()["data"]["items"][0]["progress"] is None
    response = client.patch("/api/admin/settings", json={"values": {service.INTERVAL_KEY: "25"}})
    assert response.status_code == 200
    assert any(x["key"] == service.INTERVAL_KEY and x["value"] == "25" for x in response.json()["data"])
    response = client.patch("/api/admin/settings", json={"values": {service.READ_CHUNK_KEY: "8"}})
    assert response.status_code == 200
    assert any(x["key"] == service.READ_CHUNK_KEY and x["value"] == "8" and x["value_type"] == "integer"
               for x in response.json()["data"])
    assert client.patch("/api/admin/settings", json={"values": {service.READ_CHUNK_KEY: "0"}}).status_code == 422
    app.dependency_overrides[get_current_user] = lambda: UserRecord(1, "s", "s", Role.STUDENT, "enabled", "x")
    assert client.patch("/api/admin/settings", json={"values": {service.INTERVAL_KEY: "30"}}).status_code == 403


def test_idle_scan_uses_two_queries_for_one_or_one_hundred_tasks(database, tmp_path, monkeypatch):
    """回归保护：空轮询的 SQL 次数不能随执行区任务数量线性增长。"""
    from sqlalchemy import event

    monkeypatch.setattr(service.time, "time", lambda: 1001)
    queries = []
    engine = database.kw["bind"]
    for count in (1, 100):
        with database() as db:
            for i in range(1 if count == 1 else 2, count + 1):
                task = Task(task_id=f"bulk-{i}", user_id=1, workdir="/", command="x",
                            state="running", log_path=str(tmp_path / f"bulk-{i}.log"))
                db.add(task)
                db.flush()
                db.add(TaskProgress(task_id=task.id, run_key=service.run_key(task, None), scanned_at=1000))
            db.commit()
        def record(conn, cursor, statement, params, context, many):
            queries.append(statement)
        event.listen(engine, "before_cursor_execute", record)
        try:
            service.progress_tick(database)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert len(queries) == 2
        assert all("parser_state" not in sql for sql in queries)
        queries.clear()


def test_scan_budget_is_fair_and_new_execution_is_not_delayed(database, tmp_path, monkeypatch):
    ids = [create_task(database, tmp_path, number=i)[0] for i in range(1, 13)]
    monkeypatch.setattr(service.time, "time", lambda: 1000)
    service.progress_tick(database)
    with database() as db:
        assert len(db.scalars(select(TaskProgress)).all()) == service.MAX_TASKS_PER_TICK
    monkeypatch.setattr(service.time, "time", lambda: 1005)
    service.progress_tick(database)
    with database() as db:
        assert len(db.scalars(select(TaskProgress)).all()) == 12
        db.get(Task, ids[0]).started_at += timedelta(hours=1)
        before = db.get(TaskProgress, ids[0]).version
        db.commit()
    service.progress_tick(database)
    with database() as db:
        assert db.get(TaskProgress, ids[0]).version > before


def test_lease_and_unchanged_summary_do_not_trigger_page_refresh(database, tmp_path, monkeypatch):
    from app.core.rbac import Role
    from app.services.auth_service import UserRecord
    from app.services.task_service import task_change_cursor

    task_id, path = create_task(database, tmp_path)
    monkeypatch.setattr(service.time, "time", lambda: 1000)
    service.scan_task(task_id, 60, database)
    user = UserRecord(1, "admin", "admin", Role.ADMIN, "enabled", "x")
    with database() as db:
        before = task_change_cursor(user, db)
        db.get(TaskProgress, task_id).version += 1
        db.commit()
        assert task_change_cursor(user, db) == before
    monkeypatch.setattr(service.time, "time", lambda: 1060)
    service.scan_task(task_id, 60, database)
    with database() as db:
        assert task_change_cursor(user, db) == before
    path.write_text("epochs=20\nEpoch: [0] [20/100] eta: 01:20\n")
    monkeypatch.setattr(service.time, "time", lambda: 1120)
    service.scan_task(task_id, 60, database)
    with database() as db:
        after = task_change_cursor(user, db)
        assert after["zones"]["running"] != before["zones"]["running"]
        assert after["zones"]["wait"] == before["zones"]["wait"]
        assert after["zones"]["history"] == before["zones"]["history"]


def test_list_query_does_not_load_parser_state(database, tmp_path):
    from sqlalchemy import event

    task_id, _ = create_task(database, tmp_path)
    service.scan_task(task_id, 60, database)
    statements = []
    def record(conn, cursor, statement, params, context, many):
        statements.append(statement)
    engine = database.kw["bind"]
    with database() as db:
        task = db.get(Task, task_id)
        event.listen(engine, "before_cursor_execute", record)
        try:
            assert service.load_progress([task], {}, db)[task_id]["text"]
        finally:
            event.remove(engine, "before_cursor_execute", record)
    assert all("parser_state" not in sql for sql in statements)


def test_sse_database_work_does_not_run_on_event_loop(monkeypatch):
    """直接验证执行线程，慢数据库调用不得堵住整个 API 的异步事件循环。"""
    import asyncio
    import threading
    from contextlib import nullcontext
    from app.api import tasks as api

    caller = threading.get_ident()
    threads = []
    def auth(token):
        threads.append(threading.get_ident())
        return object()
    def cursor(user, db):
        threads.append(threading.get_ident())
        return {"total": 1}
    monkeypatch.setattr(api, "get_user_by_token", auth)
    monkeypatch.setattr(api, "task_change_cursor", cursor)
    monkeypatch.setattr(api, "SessionLocal", nullcontext)
    async def check():
        async def connected():
            return False
        response = await api.stream_task_events(SimpleNamespace(is_disconnected=connected), "token")
        assert "event: tasks" in await anext(response.body_iterator)
        await response.body_iterator.aclose()
    asyncio.run(check())
    assert len(threads) == 2
    assert all(thread != caller for thread in threads)


def test_history_backfill_can_yield_between_batches(database, tmp_path):
    task_id, _ = create_task(database, tmp_path, "succeeded")
    assert backfill_task_durations(database, max_batches=1) is False
    assert backfill_task_durations(database, max_batches=1) is True


def _progress_process_probe(stop):
    """子进程只写测试标记，绝不访问真实数据库或日志；首轮退出以检验自动恢复。"""
    import os
    path = Path(os.environ["NEBULAGRID_TEST_PROGRESS_PROBE"])
    if not path.exists():
        path.write_text("restart")
        return
    path.write_text(str(os.getpid()))
    stop.wait(15)
    path.write_text("stopped")


def test_scanner_is_a_managed_separate_process(tmp_path, monkeypatch):
    import os
    import time
    from app.workers import task_progress as worker

    marker = tmp_path / "child-probe"
    monkeypatch.setenv("NEBULAGRID_TEST_PROGRESS_PROBE", str(marker))
    monkeypatch.setattr(worker, "run_progress_worker", _progress_process_probe)
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.05)
    app = SimpleNamespace(state=SimpleNamespace())
    worker.start_progress_worker(app)
    original = app.state.task_progress_thread
    worker.start_progress_worker(app)
    assert app.state.task_progress_thread is original
    try:
        deadline = time.monotonic() + 12
        value = ""
        while time.monotonic() < deadline:
            value = marker.read_text() if marker.exists() else ""
            if value.isdigit():
                break
            time.sleep(0.02)
        assert value.isdigit()
        assert int(value) != os.getpid()
    finally:
        worker.stop_progress_worker(app)
    assert not original.is_alive()
    assert marker.read_text() == "stopped"


def test_gpu_remaining_time_uses_all_unreleased_occupants(database, tmp_path, monkeypatch):
    from app.schemas.nodes import GpuInfo, NodeInfo
    from app.services.dashboard_service import annotate_gpu_occupancy

    ids = [create_task(database, tmp_path, number=i)[0] for i in (1, 2)]
    node = NodeInfo(id=1, name="node", ip="127.0.0.2", ssh_user="x", state="online", scheduling_enabled=True,
                    gpus=[GpuInfo(id=10, gpu_index=0, model="GPU", total_vram_mb=16000)])
    monkeypatch.setattr(service.time, "time", lambda: 1000)
    with database() as db:
        for task_id, seconds in zip(ids, (100, 300)):
            allocation = TaskAllocation(task_id=task_id, node_id=1, gpu_ids=[10], allocation_mode="reuse")
            db.add(allocation)
            db.flush()
            task = db.get(Task, task_id)
            db.add(TaskProgress(task_id=task_id, run_key=service.run_key(task, allocation.id),
                                summary={"remaining_seconds": seconds, "scope": "task", "updated_at": 1000}))
        db.commit()
        annotate_gpu_occupancy([node], db)
        assert node.gpus[0].remaining_occupancy_seconds == 300
        for invalid in ({"catchup": {"percent": 50}}, {"scope": "stage"}, {"stale": True},
                        {"updated_at": 100}, {"remaining_seconds": 0}, {"remaining_seconds": None}):
            db.get(TaskProgress, ids[0]).summary = {"remaining_seconds": 100, "scope": "task", "updated_at": 1000, **invalid}
            db.commit()
            annotate_gpu_occupancy([node], db)
            assert node.gpus[0].scheduled_occupied
            assert node.gpus[0].remaining_occupancy_seconds is None
        allocation = db.scalar(select(TaskAllocation).where(TaskAllocation.task_id == ids[0]))
        allocation.released_at = local_datetime()
        db.commit()
        annotate_gpu_occupancy([node], db)
        assert node.gpus[0].remaining_occupancy_seconds == 300
        db.get(Task, ids[1]).state = "cancelling"
        db.commit()
        annotate_gpu_occupancy([node], db)
        assert node.gpus[0].remaining_occupancy_seconds is None


def test_gpu_availability_labels_follow_overview_thresholds(database):
    from app.schemas.nodes import GpuInfo, NodeInfo
    from app.services.dashboard_service import annotate_gpu_occupancy

    node = NodeInfo(id=1, name="node", ip="127.0.0.2", ssh_user="x", state="online", scheduling_enabled=True,
                    gpus=[GpuInfo(id=i, gpu_index=i, model="GPU", total_vram_mb=10000,
                                  free_vram_mb=free, gpu_usage=usage) for i, (free, usage) in enumerate(
                                      [(8000, 20), (7999, 0), (9000, 78), (None, None)])])
    with database() as db:
        annotate_gpu_occupancy([node], db)
        assert [g.occupancy_status for g in node.gpus] == ["available", "external", "external", "available"]
        node.state = "offline"
        annotate_gpu_occupancy([node], db)
        assert all(g.occupancy_status == "unavailable" for g in node.gpus)


def test_overview_gpu_eta_respects_node_visibility_without_exposing_tasks(database, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.core.rbac import Role
    from app.db.models import Node, Gpu
    from app.services.auth_service import UserRecord
    from app.services import node_service
    from app.services.metrics_service import LatestMetrics

    monkeypatch.setattr(node_service, "load_latest_metrics", lambda nodes: LatestMetrics())
    monkeypatch.setattr(service.time, "time", lambda: 1000)
    task_id, _ = create_task(database, tmp_path)
    with database() as db:
        db.add_all([Node(id=1, name="public-gpu", ip="127.0.0.2", ssh_user="x", state="online",
                         access_scope="public", scheduling_enabled=True),
                    Node(id=2, name="private-gpu", ip="127.0.0.3", ssh_user="x", state="online",
                         access_scope="private", sharing_scope="none", owner_user_ids=[999], scheduling_enabled=True)])
        db.add_all([Gpu(id=i, node_id=i, gpu_index=0, model="GPU", total_vram_mb=10000, schedulable=True) for i in (1, 2)])
        allocation = TaskAllocation(task_id=task_id, node_id=1, gpu_ids=[1])
        db.add(allocation)
        db.flush()
        task = db.get(Task, task_id)
        task.command = "secret-other-command"
        task.user_id = 999
        db.add(TaskProgress(task_id=task_id, run_key=service.run_key(task, allocation.id),
                            summary={"remaining_seconds": 600, "scope": "task", "updated_at": 1000}))
        db.commit()
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: UserRecord(1, "student", "student", Role.STUDENT, "enabled", "x")
    def session():
        with database() as db:
            yield db
    app.dependency_overrides[get_db] = session
    response = TestClient(app).get("/api/nodes?include_occupancy=true")
    assert response.status_code == 200
    nodes = response.json()["data"]
    assert [n["id"] for n in nodes] == [1]
    assert nodes[0]["gpus"][0]["remaining_occupancy_seconds"] == 600
    assert "secret-other-command" not in response.text
    assert "parser_state" not in response.text
    assert "task_id" not in response.text
