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

