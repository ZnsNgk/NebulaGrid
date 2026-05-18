from fastapi import Header

from app.core.security import parse_authorization_header
from app.services.auth_service import UserRecord, get_user_by_token


def get_current_user(authorization: str | None = Header(default=None)) -> UserRecord:
    """解析当前请求的 Bearer 令牌，并返回服务层可使用的用户记录。"""
    token = parse_authorization_header(authorization)
    return get_user_by_token(token)

