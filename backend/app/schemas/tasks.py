from pydantic import BaseModel, Field


class TaskRequirement(BaseModel):
    """任务资源需求，调度器后续会基于这些字段匹配节点和 GPU。"""

    need_gpus: int = Field(default=1, ge=0, le=16)
    gpu_types: list[str] = []
    node_id: int | None = None
    allow_gpu_reuse: bool = False
    max_reuse_count: int = Field(default=1, ge=1, le=16)


class TaskCreateRequest(BaseModel):
    """提交任务请求体，包含命令、工作目录、环境和资源需求。"""

    description: str = Field(default="", max_length=512)
    env_id: int | None = None
    workdir: str = Field(default="/", min_length=1, max_length=1024)
    command: str = Field(min_length=1, max_length=4096)
    priority: int = Field(default=0, ge=0, le=100)
    on_hold: bool = False
    requirement: TaskRequirement = TaskRequirement()


class TaskUpdateRequest(BaseModel):
    """编辑等待或挂起任务的请求体，所有字段均可选以支持局部修改。"""

    description: str | None = Field(default=None, max_length=512)
    env_id: int | None = None
    workdir: str | None = Field(default=None, min_length=1, max_length=1024)
    command: str | None = Field(default=None, min_length=1, max_length=4096)
    priority: int | None = Field(default=None, ge=0, le=100)
    on_hold: bool | None = None
    requirement: TaskRequirement | None = None


class TaskInfo(BaseModel):
    """任务响应模型，稳定暴露任务主表与资源需求摘要。"""

    id: int
    task_id: str
    user_id: int
    description: str
    env_id: int | None
    workdir: str
    command: str
    state: str
    priority: int
    on_hold: bool
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    requirement: TaskRequirement


class TaskGuardInfo(BaseModel):
    """运行时守护检测摘要，用于展示 PID/GPU 越权检测结果。"""

    task_id: str
    root_pid: int | None
    process_group_id: int | None
    allocated_gpu_ids: list[int]
    observed_gpu_uuids: list[str]
    violation_count: int
    state: str
    last_check_at: str | None
