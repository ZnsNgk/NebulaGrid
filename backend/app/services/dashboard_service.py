from sqlalchemy.orm import Session

from app.core.rbac import require_permission
from app.schemas.dashboard import DashboardSummary
from app.services.auth_service import UserRecord
from app.services.node_service import list_nodes
from app.services.task_service import list_tasks


def build_dashboard_summary(user: UserRecord, db: Session) -> DashboardSummary:
    """构造首页统计摘要，节点资源来自数据库中的计算节点快照。"""
    require_permission(user.role, "dashboard:read")
    nodes = list_nodes(user, db)
    tasks, _ = list_tasks(user, state=None, search=None, page=1, page_size=200)
    gpus_total = sum(len(node.gpus) for node in nodes)
    return DashboardSummary(
        nodes_total=len(nodes),
        nodes_online=sum(1 for node in nodes if node.state == "online"),
        gpus_total=gpus_total,
        gpus_available=sum(
            1
            for node in nodes
            if node.state == "online" and node.scheduling_enabled
            for gpu in node.gpus
            if gpu.schedulable
        ),
        tasks_waiting=sum(1 for task in tasks if task.state in {"wait", "on_hold"}),
        tasks_running=sum(1 for task in tasks if task.state == "running"),
        tasks_finished_today=sum(1 for task in tasks if task.state in {"succeeded", "failed", "cancelled"}),
        viewer_role=user.role.value,
    )
