import logging

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Node
from app.db.session import SessionLocal
from app.workers.common import run_forever

logger = logging.getLogger(__name__)


def node_monitor_tick() -> None:
    """扫描已登记节点并记录状态，真实 SSH 指标采集后续接入 remote/monitor.py。"""
    with SessionLocal() as db:
        nodes = db.scalars(select(Node).order_by(Node.id)).all()
        logger.info("node monitor observed %s nodes", len(nodes))


def main() -> None:
    """节点监控 worker 入口，供 systemd 独立管理。"""
    run_forever("nebulagrid-monitor", get_settings().monitor_interval_seconds, node_monitor_tick)


if __name__ == "__main__":
    main()

