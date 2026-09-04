from pydantic import BaseModel, Field


class FileEntry(BaseModel):
    """文件列表项，避免前端直接依赖服务器真实路径。"""

    name: str
    path: str
    type: str
    size_bytes: int
    modified_at: str | None = None


class FileListData(BaseModel):
    """目录列表响应数据，path 用于后续操作，display_path 只负责前端友好展示。"""

    path: str
    display_path: str | None = None
    items: list[FileEntry]


class FilePreviewData(BaseModel):
    """文件预览响应数据；不支持在线预览的类型只返回元数据，不读取文件内容。"""

    path: str
    content_type: str
    content: str
    encoding: str = "text"
    previewable: bool = True
    truncated: bool
    size_bytes: int
    converted_from: str | None = None
    content_bytes: int = 0
    preview_limit_bytes: int | None = None
    full_content: bool = False
    can_save: bool = False
    mode_octal: str
    owner_executable: bool
    group_executable: bool
    other_executable: bool
    main_user: str
    main_user_can_execute: bool


class FilePermissionData(BaseModel):
    """文件权限面板使用的精简元数据，避免前端为了刷新权限重新读取大文件内容。"""

    path: str
    mode_octal: str
    owner_executable: bool
    group_executable: bool
    other_executable: bool
    main_user: str
    main_user_can_execute: bool


class FileOperationRequest(BaseModel):
    """通用文件操作请求体，path 是源路径，target_path 是目标路径。"""

    path: str = Field(min_length=1, max_length=1024)
    target_path: str | None = Field(default=None, max_length=1024)
    scope: str | None = Field(default=None, max_length=32)
    target_scope: str | None = Field(default=None, max_length=32)


class FileContentRequest(BaseModel):
    """文本文件写入请求体，只用于 UTF-8 可编辑内容，避免误写二进制文件。"""

    path: str = Field(min_length=1, max_length=1024)
    # 与 2 MiB 在线编辑阈值对齐；更大的文件可主动完整加载，但不允许直接覆盖保存。
    content: str = Field(default="", max_length=2 * 1024 * 1024)


class FileJobData(BaseModel):
    """文件打包/解压任务进度；服务端保留最近一次任务供刷新后恢复显示。"""

    id: str
    action: str
    source_path: str
    target_path: str
    state: str
    progress: int
    current_file: str = ""
    message: str = ""
    created_at: str
    updated_at: str
    finished_at: str | None = None
