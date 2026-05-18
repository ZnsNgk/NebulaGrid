import logging

from sqlalchemy import select

from app.db.models import TaskRuntimeGuard
from app.db.session import SessionLocal
from app.workers.common import run_forever

logger = logging.getLogger(__name__)


def runtime_guard_tick() -> None:
    """扫描运行时守护记录，后续会比对 PID 树和实际 GPU 使用。"""
    with SessionLocal() as db:
        guards = db.scalars(select(TaskRuntimeGuard).limit(20)).all()
        logger.info("runtime guard observed %s guard records", len(guards))


def main() -> None:
    """运行时守护 worker 入口，负责发现 GPU 越权并标记 alloc_error。"""
    run_forever("nebulagrid-guard", 5, runtime_guard_tick)


if __name__ == "__main__":
    main()

