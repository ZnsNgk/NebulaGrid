from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.rbac import require_permission
from app.db.models import Node, Task
from app.schemas.dashboard import (
    DashboardSummary,
    PresenterDashboard,
    PresenterGpuInfo,
    PresenterNodeInfo,
    PresenterSummary,
)
from app.services.auth_service import UserRecord
from app.services.metrics_service import HistoricalMetrics, get_historical_metrics
from app.services.node_service import (
    build_node_info,
    is_control_plane_node,
    list_nodes,
    load_latest_metrics,
    load_occupied_gpu_ids,
)
from app.services.task_service import RUNNING_STATES, TERMINAL_STATES, WAIT_STATES, list_tasks

AVAILABLE_GPU_MAX_USAGE = 20
AVAILABLE_GPU_MIN_FREE_VRAM_RATIO = 0.80


def build_dashboard_summary(user: UserRecord, db: Session) -> DashboardSummary:
    """构造首页统计摘要，节点资源来自数据库中的计算节点快照。"""
    require_permission(user.role, "dashboard:read")
    # 共享范围只影响用户能看到和使用的 GPU；节点总量等平台级指标仍按全量计算节点统计。
    nodes = list_nodes(user, db, visible_only=False)
    visible_nodes = list_nodes(user, db, visible_only=True)
    tasks, _ = list_tasks(user, db, state=None, search=None, page=1, page_size=200)
    gpus_total = sum(1 for node in nodes for gpu in node.gpus if gpu.schedulable)
    return DashboardSummary(
        nodes_total=len(nodes),
        nodes_online=sum(1 for node in nodes if node.state == "online"),
        gpus_total=gpus_total,
        gpus_available=count_available_gpus(visible_nodes),
        tasks_waiting=sum(1 for task in tasks if task.state in {"wait", "on_hold"}),
        # 停止确认期间任务仍持有远端进程和 allocation，因此继续计入执行中数量。
        tasks_running=sum(1 for task in tasks if task.state in RUNNING_STATES),
        tasks_finished_today=sum(1 for task in tasks if task.state in TERMINAL_STATES),
        viewer_role=user.role.value,
    )


def count_available_gpus(nodes: list) -> int:
    """统计当前可被调度拿走的 GPU，不把已占用或监控显示高负载的卡计入首页可用量。"""
    return sum(
        1
        for node in nodes
        if node.state == "online" and node.scheduling_enabled
        for gpu in node.gpus
        if gpu_is_summary_available(gpu)
    )


def gpu_is_summary_available(gpu) -> bool:
    """首页可用量面向独占调度口径：先排除调度占用，再用实时负载避免把外部高占用卡算作空闲。"""
    if not gpu.schedulable or gpu.scheduled_occupied:
        return False
    if gpu.gpu_usage is not None and gpu.gpu_usage > AVAILABLE_GPU_MAX_USAGE:
        return False
    if gpu.free_vram_mb is not None and gpu.total_vram_mb > 0:
        free_ratio = gpu.free_vram_mb / gpu.total_vram_mb
        if free_ratio < AVAILABLE_GPU_MIN_FREE_VRAM_RATIO:
            return False
    return True


def build_presenter_dashboard(user: UserRecord, db: Session, history_hours: int = 1) -> PresenterDashboard:
    """构造展示者大屏数据；只读聚合接口不复用普通任务/节点列表权限。"""
    require_permission(user.role, "presenter:read")
    nodes = db.scalars(
        select(Node)
        .options(selectinload(Node.gpus))
        .order_by(Node.id)
    ).all()
    compute_nodes = [node for node in nodes if not is_control_plane_node(node)]
    latest_metrics = load_latest_metrics(compute_nodes)
    occupied_gpu_ids = load_occupied_gpu_ids(compute_nodes, db)
    historical_metrics = load_presenter_history(compute_nodes, history_hours=history_hours)
    node_infos = [build_node_info(node, latest_metrics, occupied_gpu_ids) for node in compute_nodes]
    return PresenterDashboard(
        summary=PresenterSummary(
            nodes_total=len(node_infos),
            nodes_online=sum(1 for node in node_infos if node.state == "online"),
            gpus_total=sum(1 for node in node_infos for gpu in node.gpus if gpu.schedulable),
            tasks_waiting=count_tasks_by_states(db, WAIT_STATES),
            tasks_running=count_tasks_by_states(db, RUNNING_STATES),
            tasks_history_total=count_tasks_by_states(db, TERMINAL_STATES),
        ),
        nodes=[
            PresenterNodeInfo(
                id=node.id,
                name=node.name,
                state=node.state,
                scheduling_enabled=node.scheduling_enabled,
                max_speed_mbps=node.max_speed_mbps,
                cpu_usage=node.cpu_usage,
                avail_ram_mb=node.avail_ram_mb,
                network_bandwidth_mbps=node.network_bandwidth_mbps,
                upload_mbps=node.upload_mbps,
                download_mbps=node.download_mbps,
                metric_collected_at=node.metric_collected_at,
                history=metric_history_to_payload(historical_metrics.nodes.get(node.id, {})),
                gpus=[
                    PresenterGpuInfo(
                        id=gpu.id,
                        gpu_index=gpu.gpu_index,
                        gpu_uuid=gpu.gpu_uuid,
                        model=gpu.model,
                        total_vram_mb=gpu.total_vram_mb,
                        schedulable=gpu.schedulable,
                        scheduled_occupied=gpu.scheduled_occupied,
                        free_vram_mb=gpu.free_vram_mb,
                        gpu_usage=gpu.gpu_usage,
                        process_count=gpu.process_count,
                        metric_collected_at=gpu.metric_collected_at,
                        history=metric_history_to_payload(historical_metrics.gpus.get(gpu.id, {})),
                    )
                    for gpu in node.gpus
                    if gpu.schedulable
                ],
            )
            for node in node_infos
        ],
    )


def load_presenter_history(nodes: list[Node], history_hours: int = 1) -> HistoricalMetrics:
    """展示大屏依赖 InfluxDB 历史数据；查询失败时降级为空曲线，避免阻断登录页面。"""
    node_ids = [node.id for node in nodes]
    gpu_ids = [gpu.id for node in nodes for gpu in node.gpus]
    try:
        return get_historical_metrics(node_ids, gpu_ids, hours=history_hours)
    except Exception:
        return HistoricalMetrics()


def count_tasks_by_states(db: Session, states: set[str]) -> int:
    """按任务状态集合做全局计数，大屏只展示数量，不返回任何用户任务内容。"""
    return db.scalar(select(func.count()).select_from(Task).where(Task.state.in_(states))) or 0


def metric_history_to_payload(history: dict[str, list]) -> dict[str, list[dict[str, int | str]]]:
    """把 dataclass 采样点转换为普通字典，保持 Pydantic 响应可序列化。"""
    return {
        field_name: [{"time": point.time, "value": point.value} for point in points]
        for field_name, points in history.items()
    }
