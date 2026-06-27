from typing import Any

from pydantic import BaseModel


class ApiResponse(BaseModel):
    """统一响应模型，用于 OpenAPI 文档展示通用字段。"""

    ok: bool
    code: str
    message: str
    data: Any | None = None
    request_id: str


class HealthData(BaseModel):
    """健康检查响应数据，供部署和监控系统识别服务版本。"""

    service: str
    version: str
    environment: str
    status: str


class RuntimeConfigData(BaseModel):
    """前端运行时配置；只暴露登录用户提交任务和查看文件时需要的非敏感路径。"""

    shared_folder_root: str

