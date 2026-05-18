from pydantic import BaseModel


class ManualDocument(BaseModel):
    """前端使用手册响应模型，content 保存原始 Markdown 方便浏览器端渲染。"""

    title: str
    role: str
    source_path: str
    content: str
