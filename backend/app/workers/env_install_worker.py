import logging

from sqlalchemy import select

from app.db.models import EnvInstallJob
from app.db.session import SessionLocal
from app.workers.common import run_forever

logger = logging.getLogger(__name__)


def env_install_tick() -> None:
    """扫描环境安装作业队列，真实版本会执行 normal 或 compile 安装流程。"""
    with SessionLocal() as db:
        jobs = db.scalars(select(EnvInstallJob).where(EnvInstallJob.status == "queued").limit(10)).all()
        logger.info("env install worker observed %s queued jobs", len(jobs))


def main() -> None:
    """环境安装 worker 入口，与普通训练任务队列隔离运行。"""
    run_forever("nebulagrid-envworker", 5, env_install_tick)


if __name__ == "__main__":
    main()

