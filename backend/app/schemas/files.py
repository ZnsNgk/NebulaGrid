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
    """文件预览响应数据，文本直接返回内容，二进制用 base64 片段展示。"""

    path: str
    content_type: str
    content: str
    encoding: str = "text"
    truncated: bool
    size_bytes: int


class FileOperationRequest(BaseModel):
    """通用文件操作请求体，path 是源路径，target_path 是目标路径。"""

    path: str = Field(min_length=1, max_length=1024)
    target_path: str | None = Field(default=None, max_length=1024)


class FileContentRequest(BaseModel):
    """文本文件写入请求体，只用于 UTF-8 可编辑内容，避免误写二进制文件。"""

    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(default="", max_length=2_000_000)


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
