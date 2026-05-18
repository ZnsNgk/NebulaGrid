import hmac
from hashlib import sha256

from app.core.errors import unauthorized

TOKEN_PREFIX = "demo"


def hash_password(password: str) -> str:
    """生成演示用密码摘要；正式版本会替换为带随机盐的密码哈希方案。"""
    return sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    """使用常量时间比较校验密码摘要，降低简单时序侧信道风险。"""
    return hmac.compare_digest(hash_password(password), password_hash)


def create_session_token(username: str) -> str:
    """签发可读的演示令牌，后续接入 JWT 或服务端 session 时替换这里。"""
    digest = sha256(f"{TOKEN_PREFIX}:{username}".encode("utf-8")).hexdigest()[:24]
    return f"{TOKEN_PREFIX}.{username}.{digest}"


def parse_authorization_header(value: str | None) -> str:
    """解析 Bearer 令牌，并在缺失或格式错误时抛出统一未认证异常。"""
    if not value:
        raise unauthorized("missing Authorization header")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized("invalid Authorization header")
    return token


def verify_session_token(token: str, username: str) -> bool:
    """校验演示令牌是否与指定用户匹配，避免客户端随意伪造用户名。"""
    return hmac.compare_digest(token, create_session_token(username))

