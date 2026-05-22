from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class GpuInfo(BaseModel):
    """节点 GPU 子资源信息，用于列表和详情接口展示。"""

    id: int
    gpu_index: int
    gpu_uuid: str = ""
    model: str
    total_vram_mb: int
    schedulable: bool = True
    scheduled_occupied: bool = False
    remark: str | None = None
    free_vram_mb: int | None = None
    gpu_usage: int | None = None
    process_count: int | None = None
    metric_collected_at: str | None = None


class NodeInfo(BaseModel):
    """计算节点信息，覆盖管理员节点列表和用户总览可见性字段。"""

    id: int
    name: str
    ip: str
    ssh_user: str
    owner_type: str = "public"
    owner_user_id: int | None = None
    owner_user_ids: list[int] = Field(default_factory=list)
    access_scope: str = "public"
    sharing_scope: str = "public"
    is_public: bool = True
    max_speed_mbps: int | None = None
    state: str
    scheduling_enabled: bool
    gpus: list[GpuInfo] = Field(default_factory=list)
    cpu_usage: int | None = None
    avail_ram_mb: int | None = None
    network_bandwidth_mbps: int | None = None
    upload_mbps: int | None = None
    download_mbps: int | None = None
    metric_collected_at: str | None = None


class NodeSaveRequest(BaseModel):
    """新增或修改节点请求体，GPU 型号顺序必须与 nvidia-smi 输出一致。"""

    name: str = Field(min_length=1, max_length=64)
    ip: str = Field(min_length=1, max_length=128)
    ssh_user: str = Field(default="ddltm", min_length=1, max_length=64)
    owner_user_id: int | None = Field(default=None, ge=1)
    owner_user_ids: list[int] = Field(default_factory=list)
    access_scope: str = Field(default="public", pattern="^(public|private)$")
    sharing_scope: str = Field(default="public", pattern="^(none|group|public)$")
    is_public: bool | None = None
    max_speed_mbps: int | None = Field(default=None, ge=1)
    gpu_count: int | None = Field(default=None, ge=0, le=64)
    gpu_models: list[str] = Field(default_factory=list)

    @field_validator("name", "ip", "ssh_user", mode="before")
    @classmethod
    def strip_required_text(cls, value: Any) -> Any:
        """表单字段先去掉首尾空格，避免登记出不可见差异的节点。"""
        return value.strip() if isinstance(value, str) else value

    @field_validator("owner_user_ids", mode="before")
    @classmethod
    def clean_owner_ids(cls, value: Any) -> list[int]:
        """复选下拉框可能提交字符串数组，这里统一转成去重后的整数 ID。"""
        if value is None:
            return []
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        cleaned: list[int] = []
        for item in value:
            owner_id = int(item)
            if owner_id not in cleaned:
                cleaned.append(owner_id)
        return cleaned

    @field_validator("gpu_models", mode="before")
    @classmethod
    def clean_gpu_models(cls, value: Any) -> list[str]:
        """允许前端提交数组；后端只保存非空型号并保持原有顺序。"""
        if value is None:
            return []
        if isinstance(value, str):
            value = value.replace("，", ",").replace("\n", ",").split(",")
        return [str(item).strip() for item in value if str(item).strip()]

    @model_validator(mode="after")
    def validate_gpu_count(self) -> "NodeSaveRequest":
        """GPU 数量必须与型号列表逐项对应，否则调度器无法按 index 正确分配。"""
        if self.owner_user_id is not None and self.owner_user_id not in self.owner_user_ids:
            # 兼容历史单 owner 字段，最终仍统一落到 owner_user_ids 顺序列表中。
            self.owner_user_ids = [self.owner_user_id, *self.owner_user_ids]
        if self.gpu_count is None:
            self.gpu_count = len(self.gpu_models)
        if self.gpu_count != len(self.gpu_models):
            raise ValueError("gpu_count must match gpu_models length")
        if self.is_public is not None:
            self.access_scope = "public" if self.is_public else "private"
        return self


class NodeCreateRequest(NodeSaveRequest):
    """新增节点请求体，管理员通过该结构登记计算节点。"""


class NodeUpdateRequest(NodeSaveRequest):
    """修改节点请求体，复用新增字段以保证节点清单结构一致。"""
