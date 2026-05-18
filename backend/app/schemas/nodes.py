from pydantic import BaseModel, Field


class GpuInfo(BaseModel):
    """节点 GPU 子资源信息，用于列表和详情接口展示。"""

    id: int
    gpu_index: int
    model: str
    total_vram_mb: int
    schedulable: bool = True
    remark: str | None = None


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


class NodeCreateRequest(BaseModel):
    """新增节点请求体，管理员通过该结构登记计算节点。"""

    name: str = Field(min_length=1, max_length=64)
    ip: str = Field(min_length=1, max_length=128)
    ssh_user: str = Field(default="ddltm", min_length=1, max_length=64)
    is_public: bool = True
    max_speed_mbps: int | None = Field(default=None, ge=1)
    gpu_models: list[str] = []

