from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


class TimestampMixin:
    """为业务表提供统一创建时间，减少重复字段定义。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base, TimestampMixin):
    """平台用户表，保存角色、状态和 master 子账号映射。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    real_name: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(32), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), default="enabled", index=True)
    home_path: Mapped[str] = mapped_column(String(1024))
    linux_account_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linux_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linux_gid: Mapped[int | None] = mapped_column(Integer, nullable=True)


class UserSupervisor(Base):
    """学生与导师关联表，用于限制导师可见和可管理范围。"""

    __tablename__ = "user_supervisors"
    __table_args__ = (UniqueConstraint("student_id", "supervisor_id", name="uq_user_supervisor"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    supervisor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class LoginSession(Base, TimestampMixin):
    """登录会话表，后续用于多设备追踪和令牌吊销。"""

    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    login_device: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    logout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Node(Base, TimestampMixin):
    """计算节点表，记录 SSH 连接信息、归属、状态和调度开关。"""

    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    ip: Mapped[str] = mapped_column(String(128))
    ssh_user: Mapped[str] = mapped_column(String(64))
    owner_type: Mapped[str] = mapped_column(String(32), default="public", index=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    max_speed_mbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="offline", index=True)
    scheduling_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    gpus: Mapped[list["Gpu"]] = relationship(back_populates="node", cascade="all, delete-orphan")


class Gpu(Base):
    """节点 GPU 子资源表，调度器基于它做资源筛选和占用。"""

    __tablename__ = "gpus"
    __table_args__ = (UniqueConstraint("node_id", "gpu_index", name="uq_gpu_node_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    gpu_index: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(128), index=True)
    total_vram_mb: Mapped[int] = mapped_column(Integer, default=0)
    schedulable: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True)
    node: Mapped[Node] = relationship(back_populates="gpus")


class Env(Base, TimestampMixin):
    """用户 Python/conda 环境表，记录环境来源、路径和可用状态。"""

    __tablename__ = "envs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    path: Mapped[str] = mapped_column(String(1024))
    description: Mapped[str] = mapped_column(String(512), default="")
    source_type: Mapped[str] = mapped_column(String(32), default="registered")
    state: Mapped[str] = mapped_column(String(32), default="available", index=True)
    python_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)


class Task(Base, TimestampMixin):
    """任务主表，保存用户命令、状态和调度生命周期时间。"""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    env_id: Mapped[int | None] = mapped_column(ForeignKey("envs.id"), nullable=True)
    workdir: Mapped[str] = mapped_column(String(1024))
    command: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    on_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requirement: Mapped["TaskRequirement"] = relationship(back_populates="task", cascade="all, delete-orphan")


class TaskRequirement(Base):
    """任务资源需求表，描述 GPU 数量、类型、指定节点和复用策略。"""

    __tablename__ = "task_requirements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), unique=True, index=True)
    need_gpus: Mapped[int] = mapped_column(Integer, default=1)
    gpu_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    allow_gpu_reuse: Mapped[bool] = mapped_column(Boolean, default=False)
    max_reuse_count: Mapped[int] = mapped_column(Integer, default=1)
    task: Mapped[Task] = relationship(back_populates="requirement")


class TaskDependency(Base):
    """任务依赖表，用于表达前驱成功后再运行等策略。"""

    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("task_id", "prev_task_id", name="uq_task_dependency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    prev_task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    policy: Mapped[str] = mapped_column(String(32), default="success")


class TaskAllocation(Base):
    """任务资源分配表，调度器用它记录 GPU 占用和释放时间。"""

    __tablename__ = "task_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), index=True)
    gpu_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    cpu_allocated: Mapped[int] = mapped_column(Integer, default=0)
    allocation_mode: Mapped[str] = mapped_column(String(32), default="exclusive")
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskEvent(Base, TimestampMixin):
    """任务事件流表，记录创建、状态变化、调度失败和人工操作。"""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AuditLog(Base, TimestampMixin):
    """审计日志表，记录危险操作和管理动作。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str] = mapped_column(String(128), index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(32), default="success")
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Setting(Base):
    """系统配置表，保存可在线修改的运维参数。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EnvPackage(Base, TimestampMixin):
    """环境包元数据表，保存上传的 wheel 或源码包信息。"""

    __tablename__ = "env_packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    env_id: Mapped[int] = mapped_column(ForeignKey("envs.id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    package_type: Mapped[str] = mapped_column(String(32))
    file_path: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="uploaded", index=True)


class EnvInstallJob(Base, TimestampMixin):
    """环境包安装作业表，与普通任务队列隔离。"""

    __tablename__ = "env_install_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("env_packages.id"), index=True)
    env_id: Mapped[int] = mapped_column(ForeignKey("envs.id"), index=True)
    mode: Mapped[str] = mapped_column(String(32), default="normal")
    target_node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    visible_gpu_indices: Mapped[list[int]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    remote_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_path: Mapped[str] = mapped_column(String(1024))
    return_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EnvPackageManifest(Base, TimestampMixin):
    """环境包安装清单表，用于审计和后续可选卸载。"""

    __tablename__ = "env_package_manifests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("env_install_jobs.id", ondelete="CASCADE"), index=True)
    env_id: Mapped[int] = mapped_column(ForeignKey("envs.id"), index=True)
    path: Mapped[str] = mapped_column(String(1024))
    action: Mapped[str] = mapped_column(String(32))
    file_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)


class EnvOperationLog(Base, TimestampMixin):
    """环境操作日志表，与落盘 JSON Lines 日志保持同源记录，便于后续检索和审计。"""

    __tablename__ = "env_operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    env_id: Mapped[int] = mapped_column(ForeignKey("envs.id", ondelete="CASCADE"), index=True)
    env_name: Mapped[str] = mapped_column(String(128), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="info", index=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    return_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    log_path: Mapped[str] = mapped_column(String(1024), default="")


class FileJob(Base):
    """文件打包和解压任务表，用于跨刷新、重启和多 worker 共享进度状态。"""

    __tablename__ = "file_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    source_path: Mapped[str] = mapped_column(String(1024))
    target_path: Mapped[str] = mapped_column(String(1024))
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    current_file: Mapped[str] = mapped_column(String(1024), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskRuntimeGuard(Base):
    """运行时守护记录表，追踪任务实际 PID/GPU 使用一致性。"""

    __tablename__ = "task_runtime_guards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), unique=True, index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), index=True)
    root_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allocated_gpu_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    observed_gpu_uuids: Mapped[list[str]] = mapped_column(JSON, default=list)
    violation_count: Mapped[int] = mapped_column(Integer, default=0)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="not_started")
