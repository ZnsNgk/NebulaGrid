import logging
import time
from collections.abc import Callable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def run_forever(name: str, interval_seconds: int, tick: Callable[[], None]) -> None:
    """以固定间隔运行 worker tick，并捕获异常避免后台进程静默退出。"""
    logger = logging.getLogger(name)
    logger.info("%s started with interval=%ss", name, interval_seconds)
    while True:
        try:
            tick()
        except Exception:
            logger.exception("%s tick failed", name)
        time.sleep(interval_seconds)

