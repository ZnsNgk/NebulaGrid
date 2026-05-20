from pydantic import BaseModel, Field


class EnvInfo(BaseModel):
    """用户环境响应模型，覆盖环境路径、来源、状态和版本信息。"""

    id: int
    owner_user_id: int
    owner_name: str | None = None
    name: str
    path: str
    can_modify: bool = False
    description: str
    source_type: str
    state: str
    python_version: str | None = None
    size_bytes: int = 0
    created_at: str


class EnvFrameworkInfo(BaseModel):
    """记录深度学习框架的检测结果；缺失或导入失败时保留错误信息，便于页面直接展示。"""

    installed: bool
    version: str | None = None
    cuda: str | None = None
    cudnn: str | int | None = None
    cuda_available: bool | None = None
    gpu_count: int | None = None
    error: str | None = None


class EnvPackageVersion(BaseModel):
    """保存环境内 Python 包的名称和版本，包列表由目标环境自身解释器采集。"""

    name: str
    version: str


class EnvTestResult(BaseModel):
    """环境检测响应模型，覆盖 Python、PyTorch、TensorFlow 和包清单。"""

    ok: bool
    env_id: int
    env_name: str
    env_path: str
    python_executable: str | None = None
    python_version: str | None = None
    pytorch: EnvFrameworkInfo
    tensorflow: EnvFrameworkInfo
    packages: list[EnvPackageVersion] = Field(default_factory=list)
    package_count: int = 0
    error: str | None = None


class EnvUploadRequest(BaseModel):
    """环境导入占位请求体，MVP 阶段记录元数据而不处理真实包文件。"""

    name: str = Field(min_length=1, max_length=128)
    path: str | None = Field(default=None, max_length=1024)
    description: str = Field(default="", max_length=512)
    python_version: str | None = Field(default=None, max_length=32)


class EnvArchiveImportRequest(BaseModel):
    """从用户根目录导入打包环境的请求体，path 指向用户选择的 zip 包虚拟路径。"""

    path: str = Field(min_length=1, max_length=1024)
    name: str | None = Field(default=None, max_length=128)
    description: str = Field(default="", max_length=512)


class EnvCloneRequest(BaseModel):
    """创建环境副本请求体，name 是复制后的新 conda 环境目录名。"""

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)


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
