from fastapi import APIRouter

from app.api import admin, auth, dashboard, envs, files, health, nodes, tasks, users


def get_api_router() -> APIRouter:
    """汇总所有 API 子路由，保持主入口只负责应用装配。"""
    router = APIRouter()
    router.include_router(health.router, tags=["health"])
    router.include_router(auth.router, prefix="/auth", tags=["auth"])
    router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
    router.include_router(nodes.router, prefix="/nodes", tags=["nodes"])
    router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
    router.include_router(files.router, prefix="/files", tags=["files"])
    router.include_router(envs.router, prefix="/envs", tags=["envs"])
    router.include_router(envs.jobs_router, prefix="/env-install-jobs", tags=["envs"])
    router.include_router(users.router, prefix="/users", tags=["users"])
    router.include_router(admin.router, prefix="/admin", tags=["admin"])
    return router
