import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Setting, Task
from app.db.session import SessionLocal
from app.workers.common import run_forever

logger = logging.getLogger(__name__)


def scheduler_tick() -> None:
    """扫描等待任务并记录候选数量，真实 GPU 事务分配会在此函数内演进。"""
    with SessionLocal() as db:
        if not scheduler_enabled(db):
            logger.info("scheduler disabled")
            return
        waiting_tasks = db.scalars(
            select(Task).where(Task.state == "wait").order_by(Task.priority.desc(), Task.created_at.asc()).limit(10)
        ).all()
        logger.info("scheduler observed %s waiting tasks", len(waiting_tasks))


def scheduler_enabled(db: Session) -> bool:
    """读取调度器开关，缺失时默认启用以符合最小部署预期。"""
    setting = db.get(Setting, "scheduler.enabled")
    return setting is None or setting.value.lower() == "true"


def main() -> None:
    """调度器命令行入口，供 systemd 使用 python -m app.workers.scheduler 启动。"""
    run_forever("nebulagrid-scheduler", get_settings().scheduler_interval_seconds, scheduler_tick)


if __name__ == "__main__":
    main()

