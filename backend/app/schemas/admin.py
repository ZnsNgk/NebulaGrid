from pydantic import BaseModel, Field


class AuditLogInfo(BaseModel):
    """审计日志响应模型，记录关键管理动作的来源、目标和结果。"""

    id: int
    actor_user_id: int | None
    action: str
    target_type: str
    target_id: str
    ip: str | None
    result: str
    created_at: str
    detail_json: dict
    category: str = "other"


class SettingsUpdateRequest(BaseModel):
    """系统配置更新请求体，仅允许管理员修改受控键值。"""

    values: dict[str, str] = Field(default_factory=dict)


class SettingInfo(BaseModel):
    """系统配置项响应模型，标记最后修改人和修改时间。"""

    key: str
    value: str
    updated_by: int | None = None
    updated_at: str | None = None
