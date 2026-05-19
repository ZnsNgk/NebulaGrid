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


class AccountUpdateRequest(BaseModel):
    """用户自助更新资料请求；用户名和角色等敏感字段仍由管理员管理。"""

    real_name: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    """用户自助修改密码请求，必须提供当前密码以降低会话被盗后的风险。"""

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class LoginSessionInfo(BaseModel):
    """登录设备和 IP 响应模型，用于用户自查当前在线会话和历史登录记录。"""

    id: int
    login_ip: str
    login_device: str
    user_agent: str
    login_time: str
    last_seen_at: str
    logout_time: str | None = None
    revoked_at: str | None = None
    state: str
    current: bool = False
