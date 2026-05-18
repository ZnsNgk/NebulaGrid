from sqlalchemy import text

from app.db.session import engine


def drop_obsolete_metrics_tables() -> None:
    """删除已迁移到 InfluxDB 的 PostgreSQL 监控指标表。"""
    statements = [
        "DROP TABLE IF EXISTS gpu_metrics",
        "DROP TABLE IF EXISTS node_metrics",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == "__main__":
    drop_obsolete_metrics_tables()
