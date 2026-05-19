from typing import Any

from pydantic import BaseModel, Field, field_validator


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


class SessionOfflineRequest(BaseModel):
    """手动下线登录设备请求。"""

    session_id: int = Field(ge=1)


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

class AdminLoginSessionQuery(BaseModel):
    """管理员登录管理查询请求；统一使用 POST，避免用户标识出现在 URL 中。"""

    user_id: int | None = Field(default=None, ge=1)
    keyword: str | None = Field(default=None, max_length=128)

    @field_validator("user_id", "keyword", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class AdminLoginSessionOfflineRequest(BaseModel):
    """管理员手动下线任意用户登录设备请求。"""

    session_id: int = Field(ge=1)


class AdminOnlineUserInfo(BaseModel):
    """管理员登录管理中的在线用户摘要。"""

    id: int
    username: str
    real_name: str
    role: str
    state: str
    online_sessions: int
    login_ips: list[str] = Field(default_factory=list)
    login_devices: list[str] = Field(default_factory=list)
    last_seen_at: str | None = None


class AdminUserLoginSessions(BaseModel):
    """管理员查看某一用户上线情况的响应项。"""

    id: int
    username: str
    real_name: str
    role: str
    state: str
    sessions: list[LoginSessionInfo] = Field(default_factory=list)

