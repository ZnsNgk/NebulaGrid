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


class PresenterSummary(BaseModel):
    """展示者大屏顶部统计，只包含全局只读数量，不暴露任务明细。"""

    nodes_total: int
    nodes_online: int
    gpus_total: int
    tasks_waiting: int
    tasks_running: int
    tasks_history_total: int


class MetricPoint(BaseModel):
    """展示者大屏历史曲线采样点。"""

    time: str
    value: int


class PresenterGpuInfo(BaseModel):
    """展示者大屏 GPU 只读状态，包含最新值和历史曲线。"""

    id: int
    gpu_index: int
    gpu_uuid: str = ""
    model: str
    total_vram_mb: int
    schedulable: bool = True
    scheduled_occupied: bool = False
    free_vram_mb: int | None = None
    gpu_usage: int | None = None
    process_count: int | None = None
    metric_collected_at: str | None = None
    history: dict[str, list[MetricPoint]]


class PresenterNodeInfo(BaseModel):
    """展示者大屏节点只读状态，包含 InfluxDB 历史曲线。"""

    id: int
    name: str
    state: str
    scheduling_enabled: bool
    max_speed_mbps: int | None = None
    cpu_usage: int | None = None
    avail_ram_mb: int | None = None
    network_bandwidth_mbps: int | None = None
    upload_mbps: int | None = None
    download_mbps: int | None = None
    metric_collected_at: str | None = None
    history: dict[str, list[MetricPoint]]
    gpus: list[PresenterGpuInfo]


class PresenterDashboard(BaseModel):
    """展示者专用聚合响应，一次返回全局统计、所有节点和历史监控数据。"""

    summary: PresenterSummary
    nodes: list[PresenterNodeInfo]
