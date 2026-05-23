from typing import Any

from pydantic import BaseModel, Field, field_validator


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
    samba_enabled: bool = False
    samba_status: str = "disabled"
    samba_status_label: str = "已禁用"
    samba_last_error: str | None = None
    supervisor_ids: list[int] = Field(default_factory=list)
    supervisor_names: list[str] = Field(default_factory=list)
    created_at: str


class UserListRequest(BaseModel):
    """用户查询请求体。所有账户管理动作统一使用 POST，避免参数出现在 URL 中。"""

    user_id: int | None = Field(default=None, ge=1, description="统一识别码")
    keyword: str | None = Field(default=None, max_length=128)
    role: str | None = Field(default=None, pattern="^(student|mentor|admin|viewer)$")
    state: str | None = Field(default=None, pattern="^(enabled|disabled)$")

    @field_validator("user_id", "keyword", "role", "state", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: Any) -> Any:
        """前端筛选表单的空字符串等价于未筛选，避免触发枚举字段校验错误。"""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class UserCreateRequest(BaseModel):
    """创建用户请求体，导师只能创建学生，管理员可创建任意角色。"""

    user_id: int | None = Field(default=None, ge=1, description="统一识别码；为空时自动分配")
    username: str = Field(min_length=1, max_length=32, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    real_name: str = Field(min_length=1, max_length=128)
    role: str = Field(default="student", pattern="^(student|mentor|admin|viewer)$")
    password: str = Field(min_length=8, max_length=256)
    state: str = Field(default="enabled", pattern="^(enabled|disabled)$")
    supervisor_ids: list[int] = Field(default_factory=list, max_length=2)

    @field_validator("user_id", mode="before")
    @classmethod
    def empty_user_id_to_none(cls, value: Any) -> Any:
        """统一识别码未填写时交给服务层自动分配。"""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class UserUpdateRequest(BaseModel):
    """管理员/导师更新用户资料。"""

    user_id: int = Field(ge=1)
    username: str | None = Field(default=None, min_length=1, max_length=32, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    real_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: str | None = Field(default=None, pattern="^(student|mentor|admin|viewer)$")
    state: str | None = Field(default=None, pattern="^(enabled|disabled)$")
    samba_enabled: bool | None = None
    supervisor_ids: list[int] | None = Field(default=None, max_length=2)

    @field_validator("username", "real_name", "role", "state", mode="before")
    @classmethod
    def empty_optional_field_to_none(cls, value: Any) -> Any:
        """编辑表单里留空表示不修改该字段。"""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class UserPasswordResetRequest(BaseModel):
    """管理端重置用户密码请求。"""

    user_id: int = Field(ge=1)
    password: str = Field(min_length=8, max_length=256)


class UserDeleteRequest(BaseModel):
    """删除用户请求体。"""

    user_id: int = Field(ge=1)
