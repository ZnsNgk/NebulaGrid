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
    pytorch_version: str | None = None
    pytorch_cuda_version: str | None = None
    pytorch_arch_list: list[str] = Field(default_factory=list)
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
    arch_list: list[str] = Field(default_factory=list)
    error: str | None = None


class EnvPackageVersion(BaseModel):
    """保存环境内 Python 包的名称和版本，包列表由目标环境自身解释器采集。"""

    name: str
    version: str
    source: str = "conda"
    protected: bool = False


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
    compile_on_master: bool = False
    gpu_visibility: str = Field(default="default", pattern="^(default|cpu|gpu)$")
    visible_gpu_indices: list[int] = []


class EnvLocalPackageInstallRequest(BaseModel):
    """本机环境包安装请求；路径均为用户可见虚拟路径，后端负责安全解析。"""

    method: str = Field(pattern="^(conda|pip)$")
    pip_mode: str | None = Field(default=None, pattern="^(wheel|folder)$")
    package_path: str | None = Field(default=None, max_length=1024)
    folder_path: str | None = Field(default=None, max_length=1024)
    requirements_path: str | None = Field(default=None, max_length=1024)
    batch: bool = False
    folder_command: str = Field(default="pip", pattern="^(pip|setup_py)$")
    compile_on_master: bool = False
    target_node_id: int | None = None
    gpu_visibility: str = Field(default="default", pattern="^(default|cpu|gpu)$")
    visible_gpu_indices: list[int] = []


class EnvLocalPackageInstallResult(BaseModel):
    """本机环境包安装结果，返回命令输出摘要并指向单环境日志。"""

    ok: bool
    env_id: int
    env_name: str
    method: str
    command: str
    return_code: int
    stdout: str = ""
    stderr: str = ""
    log_path: str


class EnvInstalledPackageDeleteRequest(BaseModel):
    """环境内已安装包删除请求；后端会再次检测来源并拒绝删除核心包。"""

    package_names: list[str] = Field(min_length=1, max_length=100)


class EnvInstalledPackageDeletePreview(BaseModel):
    """删除已安装包前的确认内容，前端用 prompt 弹窗展示给用户确认。"""

    env_id: int
    env_name: str
    packages: list[EnvPackageVersion]
    commands: list[str]
    prompt: str


class EnvInstalledPackageDeleteResult(BaseModel):
    """删除已安装包的执行结果，返回命令输出摘要并指向单环境日志。"""

    ok: bool
    env_id: int
    env_name: str
    packages: list[EnvPackageVersion]
    commands: list[str]
    return_code: int
    stdout: str = ""
    stderr: str = ""
    log_path: str


class EnvInstallJobInfo(BaseModel):
    """环境包安装作业响应模型，独立于普通训练任务队列。"""

    id: int
    package_id: int
    env_id: int
    mode: str
    target_node_id: int | None
    compile_on_master: bool = False
    gpu_visibility: str = "default"
    visible_gpu_indices: list[int]
    status: str
    remote_pid: int | None = None
    log_path: str
    return_code: int | None = None
    created_by: int
    started_at: str | None = None
    finished_at: str | None = None


class EnvCompileGpuInfo(BaseModel):
    """编译目标上的 GPU 摘要；index 用于 CUDA_VISIBLE_DEVICES，型号和显存用于页面确认。"""

    index: int
    model: str = ""
    total_vram_mb: int = 0
    uuid: str = ""


class EnvCompileTargetInfo(BaseModel):
    """安装包编译目标响应；每次打开选择框时实时探测编译器和 GPU，避免展示过期环境。"""

    id: str
    node_id: int | None = None
    is_master: bool = False
    name: str
    ip: str
    ssh_user: str
    state: str = "unknown"
    compilers: dict[str, str | None] = Field(default_factory=dict)
    gpus: list[EnvCompileGpuInfo] = Field(default_factory=list)
    collected_at: str
    error: str | None = None
