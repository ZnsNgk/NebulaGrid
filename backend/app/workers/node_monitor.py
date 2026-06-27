from __future__ import annotations

import json
import logging
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.db.models import Gpu, Node, Setting
from app.db.session import SessionLocal
from app.services.metrics_service import write_monitor_snapshot
from app.services.node_service import gpu_index_schedulable, is_control_plane_node
from app.services.audit_service import record_audit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeMonitorTarget:
    """主节点为单个计算节点维护长连接时需要的稳定连接参数。"""

    node_id: int
    name: str
    ip: str
    ssh_user: str
    remote_code_root: str
    miniconda_python: str
    interval_seconds: float
    reconnect_attempts: int
    watchdog_timeout_seconds: float


class MonitorWatchdogTimeout(RuntimeError):
    """监控长连接仍存在但超过阈值没有收到有效 JSON 时抛出，外层负责断开并重连。"""

    def __init__(self, timeout_seconds: float, got_payload: bool) -> None:
        self.timeout_seconds = timeout_seconds
        self.got_payload = got_payload
        super().__init__(f"monitor watchdog timeout after {format_seconds(timeout_seconds)}s without valid payload")


class NodeMonitorWorker:
    """每个计算节点一个 SSH 长连接线程，远端 monitor.py 按间隔持续输出 JSON Lines。"""

    def __init__(self, target: NodeMonitorTarget) -> None:
        self.target = target
        self.stop_event = threading.Event()
        self.process_lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.thread = threading.Thread(
            target=self.run,
            name=f"node-monitor-{target.node_id}-{target.name}",
            daemon=True,
        )

    def start(self) -> None:
        """启动节点监控线程；线程内部负责 SSH 连接和离线落库。"""
        self.thread.start()

    def is_alive(self) -> bool:
        """返回线程是否仍在运行，供管理器清理已退出的节点线程。"""
        return self.thread.is_alive()

    def stop(self) -> None:
        """请求线程退出，并关闭阻塞中的 SSH 进程以便 readline 立刻返回。"""
        self.stop_event.set()
        with self.process_lock:
            process = self.process
        terminate_process(process)

    def join(self, timeout: float | None = None) -> None:
        """等待线程退出；管理器只做短等待，避免单个 SSH 进程拖住全局同步。"""
        self.thread.join(timeout=timeout)

    def run(self) -> None:
        """执行单个节点的长连接监控生命周期，断线后按配置次数自动重连。"""
        if target_monitor_paused(self.target.node_id):
            logger.info("node %s monitor skipped because it is paused", self.target.name)
            return
        reconnect_attempt = 0
        mark_node_reconnecting(self.target.node_id, "monitor thread starting")
        while not self.stop_event.is_set():
            if target_monitor_paused(self.target.node_id):
                return
            if reconnect_attempt > self.target.reconnect_attempts:
                mark_node_offline(
                    self.target.node_id,
                    f"monitor reconnect exhausted after {self.target.reconnect_attempts} attempts",
                    retryable=False,
                )
                record_monitor_reconnect_exhausted(self.target)
                return
            if reconnect_attempt > 0:
                mark_node_reconnecting(
                    self.target.node_id,
                    f"monitor reconnect attempt {reconnect_attempt}/{self.target.reconnect_attempts}",
                )
                record_monitor_reconnect_attempt(self.target, reconnect_attempt, "started")
            try:
                process = self.start_process()
                got_payload = self.read_monitor_stream(process, reconnect_attempt)
                if self.stop_event.is_set() or target_monitor_paused(self.target.node_id):
                    return
                return_code = process.poll()
                if reconnect_attempt > 0 and not got_payload:
                    record_monitor_reconnect_attempt(
                        self.target,
                        reconnect_attempt,
                        "failed",
                        f"monitor connection closed before payload, return_code={return_code}",
                    )
                mark_node_offline(
                    self.target.node_id,
                    f"monitor connection closed, return_code={return_code}",
                    retryable=True,
                )
                reconnect_attempt = 1 if got_payload else reconnect_attempt + 1
            except MonitorWatchdogTimeout as exc:
                if self.stop_event.is_set() or target_monitor_paused(self.target.node_id):
                    return
                logger.warning("node %s monitor watchdog timed out: %s", self.target.name, exc)
                if reconnect_attempt > 0 and not exc.got_payload:
                    record_monitor_reconnect_attempt(self.target, reconnect_attempt, "failed", str(exc))
                mark_node_offline(self.target.node_id, str(exc), retryable=True)
                if exc.got_payload:
                    reconnect_attempt = 1
                else:
                    reconnect_attempt = reconnect_attempt + 1 if reconnect_attempt > 0 else 1
            except Exception as exc:  # noqa: BLE001 - 节点线程失败只能影响本节点，不能拖垮监控进程。
                if self.stop_event.is_set() or target_monitor_paused(self.target.node_id):
                    return
                logger.warning("node %s monitor connection failed: %s", self.target.name, exc)
                if reconnect_attempt > 0:
                    record_monitor_reconnect_attempt(self.target, reconnect_attempt, "failed", str(exc))
                mark_node_offline(self.target.node_id, str(exc), retryable=True)
                reconnect_attempt = reconnect_attempt + 1 if reconnect_attempt > 0 else 1
            finally:
                with self.process_lock:
                    process = self.process
                    self.process = None
                terminate_process(process)
            if not self.wait_before_retry():
                return

    def start_process(self) -> subprocess.Popen[str]:
        """启动远端循环监控命令，并保存进程句柄供强制下线或配置变更时关闭。"""
        command = build_remote_monitor_command(self.target, loop=True)
        process = subprocess.Popen(  # noqa: S603 - 目标命令由节点配置和固定脚本路径组成，不经过本地 shell。
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with self.process_lock:
            self.process = process
        logger.info("node %s monitor connected with interval=%ss", self.target.name, self.target.interval_seconds)
        return process

    def read_monitor_stream(self, process: subprocess.Popen[str], reconnect_attempt: int = 0) -> bool:
        """持续读取远端 JSON Lines；长时间没有有效帧时主动触发 watchdog 重连。"""
        if process.stdout is None:
            raise RuntimeError("monitor process stdout is not available")
        line_queue: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(
            target=enqueue_monitor_stdout,
            args=(process.stdout, line_queue),
            name=f"node-monitor-reader-{self.target.node_id}-{self.target.name}",
            daemon=True,
        )
        reader.start()
        got_payload = False
        last_payload_at = time.monotonic()
        while not self.stop_event.is_set():
            if target_monitor_paused(self.target.node_id):
                self.stop_event.set()
                break
            elapsed = time.monotonic() - last_payload_at
            if elapsed >= self.target.watchdog_timeout_seconds:
                raise MonitorWatchdogTimeout(self.target.watchdog_timeout_seconds, got_payload)
            try:
                raw_line = line_queue.get(timeout=min(1.0, self.target.watchdog_timeout_seconds - elapsed))
            except queue.Empty:
                continue
            if raw_line is None:
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = parse_monitor_payload(line)
            except ValueError as exc:
                logger.warning("node %s monitor emitted invalid payload: %s", self.target.name, exc)
                continue
            if not apply_monitor_payload(self.target.node_id, payload):
                self.stop_event.set()
                break
            if not got_payload and reconnect_attempt > 0:
                record_monitor_reconnect_attempt(self.target, reconnect_attempt, "success")
            got_payload = True
            last_payload_at = time.monotonic()
        return got_payload

    def wait_before_retry(self) -> bool:
        """在重连间隔中定期检查管理员强制下线，避免维护节点被后台再次拉起。"""
        deadline = time.monotonic() + max(1.0, min(5.0, self.target.interval_seconds))
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            if target_monitor_paused(self.target.node_id):
                return False
            time.sleep(0.25)
        return not self.stop_event.is_set()


class NodeMonitorManager:
    """维护节点监控线程集合，按数据库节点清单启动、停止或替换长连接线程。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.workers: dict[int, NodeMonitorWorker] = {}

    def run_forever(self) -> None:
        """主循环只负责线程编排；实际采样周期由远端 monitor.py 的 --interval 控制。"""
        interval_seconds = normalize_monitor_interval(self.settings.monitor_interval_seconds)
        logger.info("nebulagrid-monitor started with remote interval=%ss", interval_seconds)
        try:
            while True:
                interval_seconds = self.sync_workers()
                time.sleep(min(1.0, interval_seconds))
        finally:
            self.stop_all()

    def sync_workers(self) -> float:
        """同步数据库节点清单，确保可监控节点各有一个长连接线程。"""
        targets = load_monitor_targets(self.settings)
        interval_seconds = next(iter(targets.values())).interval_seconds if targets else normalize_monitor_interval(self.settings.monitor_interval_seconds)
        target_ids = set(targets)

        for node_id, worker in list(self.workers.items()):
            target = targets.get(node_id)
            if not worker.is_alive():
                self.workers.pop(node_id, None)
                continue
            if node_id not in target_ids or target != worker.target:
                logger.info("stopping node %s monitor because target changed or paused", worker.target.name)
                worker.stop()
                worker.join(timeout=2)
                if not worker.is_alive():
                    self.workers.pop(node_id, None)

        for node_id, target in targets.items():
            worker = self.workers.get(node_id)
            if worker is not None and worker.is_alive():
                continue
            worker = NodeMonitorWorker(target)
            self.workers[node_id] = worker
            worker.start()
        return interval_seconds

    def stop_all(self) -> None:
        """停止所有节点线程，供进程退出时释放 SSH 子进程。"""
        for worker in list(self.workers.values()):
            worker.stop()
        for worker in list(self.workers.values()):
            worker.join(timeout=2)
        self.workers.clear()


def node_monitor_tick() -> None:
    """保留单次轮询入口，便于测试或临时诊断时执行一次完整采集。"""
    settings = get_settings()
    with SessionLocal() as db:
        nodes = db.scalars(select(Node).options(selectinload(Node.gpus)).order_by(Node.id)).all()
        compute_nodes = [node for node in nodes if not is_control_plane_node(node)]
        for node in compute_nodes:
            monitor_node(db, node, settings.remote_code_root, settings.miniconda_python)
        db.commit()
        logger.info("node monitor updated %s compute nodes once", len(compute_nodes))


def monitor_node(db: Session | None, node: Node, remote_code_root: str, miniconda_python: str) -> None:
    """单次采集一个节点；长连接线程之外的测试和手动诊断继续复用这个入口。"""
    if node_monitor_paused(node):
        return
    try:
        payload = fetch_remote_metrics(node, remote_code_root, miniconda_python)
    except Exception as exc:  # noqa: BLE001 - worker must survive one node failure.
        logger.warning("node %s monitor failed: %s", node.name, exc)
        node.state = "offline"
        node.scheduling_enabled = False
        return
    apply_monitor_payload_to_node(db, node, payload)


def fetch_remote_metrics(node: Node, remote_code_root: str, miniconda_python: str) -> dict[str, Any]:
    """在计算节点上执行远端监控脚本的单次模式，并解析 JSON 输出。"""
    target = NodeMonitorTarget(
        node_id=node.id or 0,
        name=node.name,
        ip=node.ip,
        ssh_user=node.ssh_user,
        remote_code_root=remote_code_root,
        miniconda_python=miniconda_python,
        interval_seconds=normalize_monitor_interval(get_settings().monitor_interval_seconds),
        reconnect_attempts=normalize_reconnect_attempts(get_settings().monitor_reconnect_attempts),
        watchdog_timeout_seconds=normalize_watchdog_timeout(get_settings().monitor_watchdog_timeout_seconds),
    )
    output = subprocess.check_output(  # noqa: S603 - SSH 参数固定，远端命令经过 POSIX quote。
        build_remote_monitor_command(target, loop=False),
        text=True,
        stderr=subprocess.STDOUT,
        timeout=20,
    )
    return parse_monitor_payload(output)


def parse_monitor_payload(output: str) -> dict[str, Any]:
    """解析远端 monitor.py 输出的一行 JSON，防止非对象 payload 污染数据库。"""
    line = output.strip().splitlines()[-1] if output.strip() else ""
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("monitor output must be a JSON object")
    return data


def apply_monitor_payload(node_id: int, payload: dict[str, Any]) -> bool:
    """把长连接收到的一帧监控数据写入数据库；返回 False 表示线程应退出。"""
    with SessionLocal() as db:
        node = db.scalar(select(Node).options(selectinload(Node.gpus)).where(Node.id == node_id))
        if node is None or is_control_plane_node(node) or node_monitor_paused(node):
            return False
        apply_monitor_payload_to_node(db, node, payload)
        db.commit()
    return True


def apply_monitor_payload_to_node(db: Session | None, node: Node, payload: dict[str, Any]) -> None:
    """更新节点在线状态、GPU 库存和 InfluxDB 指标；调用方负责提交数据库事务。"""
    node.state = "online"
    node.scheduling_enabled = True
    gpu_rows: list[tuple[Gpu, dict[str, Any]]] = []
    if db is not None:
        gpu_rows = sync_gpu_inventory(db, node, payload.get("gpus", []), bool(payload.get("gpu_probe_ok", True)))
    try:
        write_monitor_snapshot(node, payload, gpu_rows)
    except Exception as exc:  # noqa: BLE001 - 指标库失败不应影响节点心跳和调度状态。
        logger.warning("node %s metrics write failed: %s", node.name, exc)


def sync_gpu_inventory(db: Session, node: Node, gpus: Any, probe_ok: bool = True) -> list[tuple[Gpu, dict[str, Any]]]:
    """用 nvidia-smi 结果校正 GPU 清单，并按管理员 0/1 列表决定是否可调度。"""
    if not isinstance(gpus, list) or not probe_ok:
        return []
    existing = {gpu.gpu_index: gpu for gpu in node.gpus}
    seen: set[int] = set()
    metric_rows = []
    for item in gpus:
        if not isinstance(item, dict):
            continue
        index = coerce_int(item.get("index"))
        seen.add(index)
        gpu = existing.get(index)
        if gpu is None:
            gpu = Gpu(gpu_index=index, model=str(item.get("name") or "Unknown"))
            node.gpus.append(gpu)
            db.flush()
            existing[index] = gpu
        gpu.gpu_uuid = str(item.get("uuid") or gpu.gpu_uuid or "")
        gpu.model = str(item.get("name") or gpu.model or "Unknown")
        gpu.total_vram_mb = coerce_int(item.get("memory_total_mb"))
        gpu.schedulable = gpu_index_schedulable(node, index)
        metric_rows.append((gpu, item))
    for index, gpu in list(existing.items()):
        if index not in seen:
            # 硬件数量变化时让数据库清单跟随 nvidia-smi，避免旧卡继续参与统计或调度。
            if gpu in node.gpus:
                node.gpus.remove(gpu)
            db.delete(gpu)
    return metric_rows


def load_monitor_targets(settings: Settings) -> dict[int, NodeMonitorTarget]:
    """读取当前需要监控的节点；被强制下线的节点等待管理员重连后再进入清单。"""
    with SessionLocal() as db:
        interval_seconds = monitor_interval_seconds(db, settings)
        reconnect_attempts = monitor_reconnect_attempts(db, settings)
        watchdog_timeout_seconds = monitor_watchdog_timeout_seconds(db, settings)
        nodes = db.scalars(select(Node).order_by(Node.id)).all()
        targets: dict[int, NodeMonitorTarget] = {}
        for node in nodes:
            if is_control_plane_node(node) or node_monitor_paused(node):
                continue
            targets[node.id] = NodeMonitorTarget(
                node_id=node.id,
                name=node.name,
                ip=node.ip,
                ssh_user=node.ssh_user,
                remote_code_root=settings.remote_code_root,
                miniconda_python=settings.miniconda_python,
                interval_seconds=interval_seconds,
                reconnect_attempts=reconnect_attempts,
                watchdog_timeout_seconds=watchdog_timeout_seconds,
            )
        return targets


def build_remote_monitor_command(target: NodeMonitorTarget, loop: bool = True) -> list[str]:
    """构造 SSH 命令；长连接模式启用远端循环输出和 SSH keepalive。"""
    remote_script = f"{target.remote_code_root.rstrip('/')}/monitor.py"
    remote_command = f"{shlex.quote(target.miniconda_python)} -u {shlex.quote(remote_script)}"
    if loop:
        remote_command = f"{remote_command} --loop --interval {format_seconds(target.interval_seconds)}"
    keepalive_interval = max(2, min(30, int(target.interval_seconds * 2)))
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ServerAliveInterval={keepalive_interval}",
        "-o",
        "ServerAliveCountMax=2",
        f"{target.ssh_user}@{target.ip}",
        remote_command,
    ]


def mark_node_reconnecting(node_id: int, reason: str) -> None:
    """重连开始时写入 reconnecting 状态；管理员隔离节点不被自动改写。"""
    with SessionLocal() as db:
        node = db.get(Node, node_id)
        if node is None or is_control_plane_node(node) or node_monitor_paused(node):
            return
        node.state = "reconnecting"
        node.scheduling_enabled = False
        db.commit()
        logger.info("node %s marked reconnecting: %s", node.name, reason)


def mark_node_offline(node_id: int, reason: str, retryable: bool = False) -> None:
    """长连接断开时把节点标为离线；retryable=True 表示线程仍可继续自动重连。"""
    with SessionLocal() as db:
        node = db.get(Node, node_id)
        if node is None or is_control_plane_node(node) or node_monitor_paused(node):
            return
        node.state = "offline"
        # retryable 的离线来自网络波动，state 已阻止调度，但保留 scheduling_enabled 让监控线程继续重连。
        # 管理员强制下线或重试耗尽时写成 False，后续目标同步会停止该节点线程。
        node.scheduling_enabled = bool(retryable)
        db.commit()
        logger.warning("node %s marked offline: %s", node.name, reason)


def record_monitor_reconnect_attempt(
    target: NodeMonitorTarget,
    attempt_number: int,
    result: str,
    message: str | None = None,
) -> None:
    """记录监控自动重连尝试，方便管理员审计网络波动和恢复过程。"""
    try:
        record_audit(
            None,
            "node.monitor_reconnect_attempt",
            "node",
            str(target.node_id),
            result=result,
            detail_json={
                "node_id": target.node_id,
                "node_name": target.name,
                "ip": target.ip,
                "attempt": attempt_number,
                "max_attempts": target.reconnect_attempts,
                "message": message or "",
            },
        )
    except Exception as exc:  # noqa: BLE001 - 审计失败不能阻断监控线程重连。
        logger.warning("node %s reconnect audit write failed: %s", target.name, exc)


def record_monitor_reconnect_exhausted(target: NodeMonitorTarget) -> None:
    """记录自动重连次数耗尽，节点将保持离线直到管理员手动重连。"""
    try:
        record_audit(
            None,
            "node.monitor_reconnect_exhausted",
            "node",
            str(target.node_id),
            result="failed",
            detail_json={
                "node_id": target.node_id,
                "node_name": target.name,
                "ip": target.ip,
                "max_attempts": target.reconnect_attempts,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 审计失败不能阻断监控线程退出。
        logger.warning("node %s reconnect exhausted audit write failed: %s", target.name, exc)


def enqueue_monitor_stdout(stdout: Any, line_queue: queue.Queue[str | None]) -> None:
    """把阻塞式 stdout 读取放到后台线程，让主监控线程可以按 watchdog 时间主动退出。"""
    try:
        for raw_line in stdout:
            line_queue.put(raw_line)
    finally:
        line_queue.put(None)


def target_monitor_paused(node_id: int) -> bool:
    """检查节点是否已删除、变成控制节点或处于管理员隔离状态。"""
    with SessionLocal() as db:
        node = db.get(Node, node_id)
        return node is None or is_control_plane_node(node) or node_monitor_paused(node)


def terminate_process(process: subprocess.Popen[str] | None) -> None:
    """关闭本地 SSH 子进程，避免线程停止后远端监控命令继续占用连接。"""
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:  # noqa: BLE001 - 退出路径尽力清理，失败时继续 kill。
        try:
            process.kill()
        except Exception:
            pass


def node_monitor_paused(node: Node) -> bool:
    """管理员隔离的离线节点不再自动 SSH 探测，避免强制下线后被监控拉回 online。"""
    return node.state == "manual_offline" or (node.state == "offline" and not node.scheduling_enabled)


def monitor_interval_seconds(db: Session, settings: Settings | None = None) -> float:
    """读取监控采样间隔；优先使用管理员后台设置，缺失时回退环境默认值。"""
    resolved = settings or get_settings()
    setting = db.get(Setting, "monitor.interval_seconds")
    raw_value = setting.value if setting is not None else str(resolved.monitor_interval_seconds)
    return normalize_monitor_interval(raw_value)


def monitor_reconnect_attempts(db: Session, settings: Settings | None = None) -> int:
    """读取监控自动重连次数；0 表示断开后不自动重连，默认值为 3。"""
    resolved = settings or get_settings()
    setting = db.get(Setting, "monitor.reconnect_attempts")
    raw_value = setting.value if setting is not None else str(resolved.monitor_reconnect_attempts)
    return normalize_reconnect_attempts(raw_value)


def monitor_watchdog_timeout_seconds(db: Session, settings: Settings | None = None) -> float:
    """读取监控无有效输出的 watchdog 超时；超时后由节点线程关闭 SSH 并进入重连流程。"""
    resolved = settings or get_settings()
    setting = db.get(Setting, "monitor.watchdog_timeout_seconds")
    raw_value = setting.value if setting is not None else str(resolved.monitor_watchdog_timeout_seconds)
    return normalize_watchdog_timeout(raw_value)


def normalize_monitor_interval(value: Any) -> float:
    """归一化监控周期；远端循环和管理器同步都使用同一个下限。"""
    try:
        interval = float(value)
    except (TypeError, ValueError):
        interval = 5.0
    return max(1.0, interval)


def normalize_reconnect_attempts(value: Any) -> int:
    """归一化自动重连次数，避免负数或过大配置造成线程长期空转。"""
    try:
        attempts = int(float(str(value).strip()))
    except (TypeError, ValueError):
        attempts = 3
    return max(0, min(100, attempts))


def normalize_watchdog_timeout(value: Any) -> float:
    """归一化 watchdog 秒数，避免过小或过大的配置造成误判或长期无响应。"""
    try:
        timeout_seconds = float(str(value).strip())
    except (TypeError, ValueError):
        timeout_seconds = 600.0
    return max(1.0, min(86400.0, timeout_seconds))


def format_seconds(value: float) -> str:
    """格式化秒数参数，避免 5.0 这类值污染日志和文档示例。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def coerce_int(value: Any) -> int:
    """把远端脚本输出的字符串或数字安全转换为非负整数。"""
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return 0


def main() -> None:
    """节点监控 worker 入口；由 systemd 启动后持续维护每节点长连接线程。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    NodeMonitorManager(get_settings()).run_forever()


if __name__ == "__main__":
    main()
