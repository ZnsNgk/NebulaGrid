from pydantic import BaseModel, Field


class FileEntry(BaseModel):
    """文件列表项，避免前端直接依赖服务器真实路径。"""

    name: str
    path: str
    type: str
    size_bytes: int
    modified_at: str | None = None


class FileListData(BaseModel):
    """目录列表响应数据，包含当前虚拟路径和子项数组。"""

    path: str
    items: list[FileEntry]


class FilePreviewData(BaseModel):
    """文本预览响应数据，二进制和大文件后续可扩展为流式接口。"""

    path: str
    content_type: str
    content: str
    truncated: bool


class FileOperationRequest(BaseModel):
    """通用文件操作请求体，用于 mkdir、archive、extract 等占位动作。"""

    path: str = Field(min_length=1, max_length=1024)
    target_path: str | None = Field(default=None, max_length=1024)

