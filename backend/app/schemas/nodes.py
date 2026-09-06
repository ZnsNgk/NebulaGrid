from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class GpuInfo(BaseModel):
    """节点 GPU 子资源信息，用于列表和详情接口展示。"""

    id: int
    gpu_index: int
    gpu_uuid: str = ""
    model: str
    total_vram_mb: int
    compute_capability: str | None = None
    detected_compute_capability: str | None = None
    schedulable: bool = True
    scheduled_occupied: bool = False
    # 总览按当前 allocation 和进度摘要即时聚合，不在 GPU 表中保存派生状态。
    occupancy_status: str | None = None
    remaining_occupancy_seconds: int | None = None
    occupancy_estimate_rough: bool = False
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
    gpu_schedulable_flags: list[int] = Field(default_factory=list)
    gpu_compute_capability_overrides: list[str] = Field(default_factory=list)
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
    """新增或修改节点请求体，GPU 清单由 monitor 扫描，管理员只维护按 index 对齐的可用性开关。"""

    name: str = Field(min_length=1, max_length=64)
    ip: str = Field(min_length=1, max_length=128)
    ssh_user: str = Field(default="ddltm", min_length=1, max_length=64)
    owner_user_id: int | None = Field(default=None, ge=1)
    owner_user_ids: list[int] = Field(default_factory=list)
    access_scope: str = Field(default="public", pattern="^(public|private)$")
    sharing_scope: str = Field(default="public", pattern="^(none|group|public)$")
    is_public: bool | None = None
    max_speed_mbps: int | None = Field(default=None, ge=1)
    gpu_schedulable_flags: list[int] = Field(default_factory=list)
    gpu_compute_capability_overrides: list[str] = Field(default_factory=list)

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

    @field_validator("gpu_schedulable_flags", mode="before")
    @classmethod
    def clean_gpu_schedulable_flags(cls, value: Any) -> list[int]:
        """把管理员按 nvidia-smi 顺序填写的 0/1 列表转换成整数，缺失项默认不可调度。"""
        if value is None:
            return []
        if isinstance(value, str):
            value = value.replace("，", ",").replace("\n", ",").split(",")
        cleaned: list[int] = []
        for item in value:
            text = str(item).strip().lower()
            if not text:
                continue
            if text in {"1", "true"}:
                cleaned.append(1)
            elif text in {"0", "false"}:
                cleaned.append(0)
            else:
                raise ValueError("gpu schedulable flags must be 0 or 1")
        return cleaned

    @field_validator("gpu_compute_capability_overrides", mode="before")
    @classmethod
    def clean_gpu_compute_capability_overrides(cls, value: Any) -> list[str]:
        """保留空行以维持 GPU index 对齐，并限制非空值为 major.minor 形式。"""
        if value is None:
            return []
        if isinstance(value, str):
            value = value.replace("，", ",").replace("\r", "").replace("\n", ",").split(",")
        cleaned = [str(item or "").strip() for item in value]
        while cleaned and not cleaned[-1]:
            cleaned.pop()
        for item in cleaned:
            if item and (item.count(".") != 1 or not all(part.isdigit() for part in item.split("."))):
                raise ValueError("gpu compute capability must use major.minor format")
        return cleaned

    @model_validator(mode="after")
    def normalize_node_options(self) -> "NodeSaveRequest":
        """兼容历史字段并统一节点公开/私有配置。"""
        if self.owner_user_id is not None and self.owner_user_id not in self.owner_user_ids:
            # 兼容历史单 owner 字段，最终仍统一落到 owner_user_ids 顺序列表中。
            self.owner_user_ids = [self.owner_user_id, *self.owner_user_ids]
        if self.is_public is not None:
            self.access_scope = "public" if self.is_public else "private"
        return self


class NodeCreateRequest(NodeSaveRequest):
    """新增节点请求体，管理员通过该结构登记计算节点。"""


class NodeUpdateRequest(NodeSaveRequest):
    """修改节点请求体，复用新增字段以保证节点清单结构一致。"""
