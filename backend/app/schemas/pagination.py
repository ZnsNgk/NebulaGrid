from pydantic import BaseModel, Field


class Page(BaseModel):
    """通用分页响应结构，统一列表接口的返回形态。"""

    items: list
    total: int
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)

