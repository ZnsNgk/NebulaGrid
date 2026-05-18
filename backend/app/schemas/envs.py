from pydantic import BaseModel, Field


class EnvInfo(BaseModel):
    """用户环境响应模型，覆盖环境路径、来源、状态和版本信息。"""

    id: int
    owner_user_id: int
    name: str
    path: str
    description: str
    source_type: str
    state: str
    python_version: str | None = None
    size_bytes: int = 0
    created_at: str


class EnvUploadRequest(BaseModel):
    """环境导入占位请求体，MVP 阶段记录元数据而不处理真实包文件。"""

    name: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1024)
    description: str = Field(default="", max_length=512)
    python_version: str | None = Field(default=None, max_length=32)


class EnvPackageInfo(BaseModel):
    """环境包元数据响应模型，用于后续 whl/源码包安装流程。"""

    id: int
    env_id: int
    owner_user_id: int
    filename: str
    package_type: str
    file_path: str
    size_bytes: int
    sha256: str
    status: str
    created_at: str


class EnvPackageUploadRequest(BaseModel):
    """环境包上传占位请求体，后续可替换为真实 multipart 上传。"""

    filename: str = Field(min_length=1, max_length=255)
    package_type: str = Field(default="wheel", max_length=32)
    size_bytes: int = Field(default=0, ge=0)
    sha256: str = Field(default="", max_length=128)


class EnvPackageInstallRequest(BaseModel):
    """环境包安装请求体，compile 模式允许指定节点和可见 GPU。"""

    mode: str = Field(default="normal", pattern="^(normal|compile)$")
    target_node_id: int | None = None
    visible_gpu_indices: list[int] = []


class EnvInstallJobInfo(BaseModel):
    """环境包安装作业响应模型，独立于普通训练任务队列。"""

    id: int
    package_id: int
    env_id: int
    mode: str
    target_node_id: int | None
    visible_gpu_indices: list[int]
    status: str
    remote_pid: int | None = None
    log_path: str
    return_code: int | None = None
    created_by: int
    started_at: str | None = None
    finished_at: str | None = None

