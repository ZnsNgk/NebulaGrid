from pydantic import BaseModel, Field


class GpuInfo(BaseModel):
    """节点 GPU 子资源信息，用于列表和详情接口展示。"""

    id: int
    gpu_index: int
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
    """计算节点信息，覆盖文档中 nodes 表的主要字段。"""

    id: int
    name: str
    ip: str
    ssh_user: str
    owner_type: str = "public"
    owner_user_id: int | None = None
    is_public: bool = True
    max_speed_mbps: int | None = None
    state: str
    scheduling_enabled: bool
    gpus: list[GpuInfo] = []
    cpu_usage: int | None = None
    avail_ram_mb: int | None = None
    network_bandwidth_mbps: int | None = None
    upload_mbps: int | None = None
    download_mbps: int | None = None
    metric_collected_at: str | None = None


class NodeCreateRequest(BaseModel):
    """新增节点请求体，管理员通过该结构登记计算节点。"""

    name: str = Field(min_length=1, max_length=64)
    ip: str = Field(min_length=1, max_length=128)
    ssh_user: str = Field(default="ddltm", min_length=1, max_length=64)
    is_public: bool = True
    max_speed_mbps: int | None = Field(default=None, ge=1)
    gpu_models: list[str] = []
