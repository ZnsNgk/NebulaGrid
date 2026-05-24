import json
import logging
import subprocess
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.db.models import Gpu, Node
from app.db.session import SessionLocal
from app.services.metrics_service import write_monitor_snapshot
from app.services.node_service import gpu_index_schedulable, is_control_plane_node
from app.workers.common import run_forever

logger = logging.getLogger(__name__)


def node_monitor_tick() -> None:
    """通过 SSH 采集所有计算节点状态，并把监控快照写入 InfluxDB。"""
    settings = get_settings()
    with SessionLocal() as db:
        nodes = db.scalars(select(Node).options(selectinload(Node.gpus)).order_by(Node.id)).all()
        compute_nodes = [node for node in nodes if not is_control_plane_node(node)]
        for node in compute_nodes:
            monitor_node(db, node, settings.remote_code_root, settings.miniconda_python)
        db.commit()
        logger.info("node monitor updated %s compute nodes", len(compute_nodes))


def monitor_node(db: Session, node: Node, remote_code_root: str, miniconda_python: str) -> None:
    """监控单个节点，SSH 失败时只标记离线，避免中断整轮采集。"""
    if node_monitor_paused(node):
        return
    try:
        payload = fetch_remote_metrics(node, remote_code_root, miniconda_python)
    except Exception as exc:  # noqa: BLE001 - worker must survive one node failure.
        logger.warning("node %s monitor failed: %s", node.name, exc)
        node.state = "offline"
        node.scheduling_enabled = False
        return
    node.state = "online"
    node.scheduling_enabled = True
    gpu_rows = sync_gpu_inventory(db, node, payload.get("gpus", []), bool(payload.get("gpu_probe_ok", True)))
    try:
        write_monitor_snapshot(node, payload, gpu_rows)
    except Exception as exc:  # noqa: BLE001 - metrics storage failure must not break node heartbeat.
        logger.warning("node %s metrics write failed: %s", node.name, exc)


def fetch_remote_metrics(node: Node, remote_code_root: str, miniconda_python: str) -> dict[str, Any]:
    """在计算节点上执行远端监控脚本，并解析 JSON 输出。"""
    remote_script = f"{remote_code_root.rstrip('/')}/monitor.py"
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{node.ssh_user}@{node.ip}",
        f"{miniconda_python} {remote_script}",
    ]
    output = subprocess.check_output(command, text=True, stderr=subprocess.STDOUT, timeout=20)
    data = json.loads(output)
    if not isinstance(data, dict):
        raise ValueError("monitor output must be a JSON object")
    return data


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


def node_monitor_paused(node: Node) -> bool:
    """管理员隔离的离线节点不再自动 SSH 探测，避免强制下线后被下一轮监控重新拉回 online。"""
    return node.state == "manual_offline" or (node.state == "offline" and not node.scheduling_enabled)


def coerce_int(value: Any) -> int:
    """把远端脚本输出的字符串/数字安全转换为非负整数。"""
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return 0


def main() -> None:
    """节点监控 worker 入口，供 systemd 独立管理。"""
    run_forever("nebulagrid-monitor", get_settings().monitor_interval_seconds, node_monitor_tick)


if __name__ == "__main__":
    main()
