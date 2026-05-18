from pydantic import BaseModel


class DashboardSummary(BaseModel):
    """首页概览数据模型，字段先稳定下来以便前端并行开发。"""

    nodes_total: int
    nodes_online: int
    gpus_total: int
    gpus_available: int
    tasks_waiting: int
    tasks_running: int
    tasks_finished_today: int
    viewer_role: str

