from pydantic import BaseModel, Field


class UserInfo(BaseModel):
    """用户列表响应模型，避免暴露密码摘要等敏感字段。"""

    id: int
    username: str
    real_name: str
    role: str
    state: str
    home_path: str
    linux_account_name: str | None = None
    linux_uid: int | None = None
    linux_gid: int | None = None
    avatar: str | None = None
    created_at: str


class UserCreateRequest(BaseModel):
    """创建用户请求体，导师只能创建学生，管理员可创建任意角色。"""

    username: str = Field(min_length=1, max_length=32, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    real_name: str = Field(min_length=1, max_length=128)
    role: str = Field(default="student", pattern="^(student|mentor|admin|viewer)$")
    password: str = Field(min_length=8, max_length=256)
    state: str = Field(default="enabled", pattern="^(enabled|disabled)$")
