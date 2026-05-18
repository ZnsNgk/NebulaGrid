from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """登录请求体，identity 允许用户名、用户 ID 或真实姓名扩展匹配。"""

    identity: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class PublicUser(BaseModel):
    """对前端安全暴露的用户字段，避免返回密码摘要等敏感信息。"""

    id: int
    username: str
    real_name: str
    role: str
    state: str
    permissions: list[str]


class LoginResult(BaseModel):
    """登录成功响应，包含 Bearer 令牌和当前用户摘要。"""

    access_token: str
    token_type: str = "bearer"
    user: PublicUser

