import logging

from sqlalchemy import select

from app.db.models import Task
from app.db.session import SessionLocal
from app.workers.common import run_forever

logger = logging.getLogger(__name__)


def task_executor_tick() -> None:
    """扫描 dispatching 任务，真实版本会通过 SSH 调用远端 runner 启动进程。"""
    with SessionLocal() as db:
        tasks = db.scalars(select(Task).where(Task.state == "dispatching").limit(10)).all()
        logger.info("executor observed %s dispatching tasks", len(tasks))


def main() -> None:
    """任务执行器入口，后续负责 SSH 启动、停止和日志归档。"""
    run_forever("nebulagrid-executor", 3, task_executor_tick)


if __name__ == "__main__":
    main()

