from typing import Any

from pydantic import BaseModel, Field, field_validator


class TaskRequirement(BaseModel):
    """任务资源需求，调度器基于这些字段匹配节点和 GPU。"""

    need_gpus: int = Field(default=1, ge=0, le=16)
    gpu_types: list[str] = Field(default_factory=list)
    node_id: int | None = Field(default=None, ge=1)
    allow_gpu_reuse: bool = False
    max_reuse_count: int = Field(default=1, ge=1, le=16)

    @field_validator("gpu_types", mode="before")
    @classmethod
    def clean_gpu_types(cls, value: Any) -> list[str]:
        """兼容前端提交的逗号字符串，并去掉空白型号，避免调度器匹配到空条件。"""
        if value is None:
            return []
        if isinstance(value, str):
            value = value.replace("，", ",").replace("\n", ",").split(",")
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned


class TaskCreateRequest(BaseModel):
    """提交单个任务的请求体，命令必须是一行，避免前端单任务入口含糊执行多条命令。"""

    description: str = Field(default="", max_length=512)
    env_id: int | None = Field(default=None, ge=1)
    workdir: str = Field(default="/", min_length=1, max_length=1024)
    command: str = Field(min_length=1, max_length=4096)
    priority: int = Field(default=0, ge=0, le=100)
    urgent: bool = False
    on_hold: bool = False
    predecessor_task_id: str | None = Field(default=None, max_length=64)
    requirement: TaskRequirement = Field(default_factory=TaskRequirement)

    @field_validator("command")
    @classmethod
    def command_must_be_single_line(cls, value: str) -> str:
        """单任务命令不允许换行，批量入口会先拆成独立命令再逐条落库。"""
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("command must be a single line")
        return text

    @field_validator("description", "workdir", "predecessor_task_id", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> Any:
        """去掉表单字段首尾空白，避免路径和 ID 出现不可见差异。"""
        return value.strip() if isinstance(value, str) else value


class TaskBatchCreateRequest(BaseModel):
    """批量提交任务的请求体，commands 每个有效行会生成一条任务。"""

    description: str = Field(default="", max_length=512)
    env_id: int | None = Field(default=None, ge=1)
    workdir: str = Field(default="/", min_length=1, max_length=1024)
    commands: str = Field(min_length=1, max_length=65535)
    priority: int = Field(default=0, ge=0, le=100)
    urgent: bool = False
    on_hold: bool = True
    predecessor_task_id: str | None = Field(default=None, max_length=64)
    requirement: TaskRequirement = Field(default_factory=TaskRequirement)

    @field_validator("description", "workdir", "predecessor_task_id", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> Any:
        """去掉批量表单字段空白，实际命令行会在服务层逐行清理注释。"""
        return value.strip() if isinstance(value, str) else value


class TaskUpdateRequest(BaseModel):
    """编辑任务的请求体，未提交字段保持原值，提交 null 的前驱任务表示清除依赖。"""

    description: str | None = Field(default=None, max_length=512)
    env_id: int | None = Field(default=None, ge=1)
    workdir: str | None = Field(default=None, min_length=1, max_length=1024)
    command: str | None = Field(default=None, min_length=1, max_length=4096)
    priority: int | None = Field(default=None, ge=0, le=100)
    urgent: bool | None = None
    on_hold: bool | None = None
    predecessor_task_id: str | None = Field(default=None, max_length=64)
    requirement: TaskRequirement | None = None

    @field_validator("command")
    @classmethod
    def command_must_be_single_line(cls, value: str | None) -> str | None:
        """修改任务时同样限制为单行命令，防止一条任务变成隐式脚本。"""
        if value is None:
            return value
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("command must be a single line")
        return text

    @field_validator("description", "workdir", "predecessor_task_id", mode="before")
    @classmethod
    def strip_optional_text(cls, value: Any) -> Any:
        """统一清理可选文本字段，保持数据库内展示值稳定。"""
        return value.strip() if isinstance(value, str) else value


class TaskInfo(BaseModel):
    """任务响应模型，合并主表、资源需求、前驱和当前分配摘要。"""

    id: int
    task_id: str
    user_id: int
    owner_name: str = ""
    owner_username: str = ""
    description: str
    env_id: int | None
    env_name: str | None = None
    env_path: str | None = None
    workdir: str
    command: str
    state: str
    priority: int
    urgent: bool = False
    on_hold: bool
    last_block_reason: str = ""
    log_path: str = ""
    return_code: int | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    predecessor_task_id: str | None = None
    predecessor_task_no: str | None = None
    node_id: int | None = None
    node_name: str | None = None
    gpu_indices: list[int] = Field(default_factory=list)
    gpu_models: list[str] = Field(default_factory=list)
    requirement: TaskRequirement


class TaskBatchCreateResult(BaseModel):
    """批量提交结果，返回实际落库的任务和被保留的命令数量。"""

    items: list[TaskInfo]
    total: int


class TaskDeletePreview(BaseModel):
    """删除确认前返回后继任务 ID，让前端给用户做二次确认。"""

    task_id: str
    successors: list[str]


class TaskDeleteResult(BaseModel):
    """删除任务后的结果摘要，便于审计和前端提示。"""

    removed: list[str]
    skipped_running: list[str] = Field(default_factory=list)


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
