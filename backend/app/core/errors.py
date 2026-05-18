from typing import Any


class AppError(Exception):
    """承载业务错误码、HTTP 状态码和调试数据，供统一异常处理器消费。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        data: Any | None = None,
    ) -> None:
        """初始化业务异常，调用方只传领域语义而不直接拼响应体。"""
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data


def unauthorized(message: str = "unauthorized") -> AppError:
    """创建未认证错误，统一映射为 401。"""
    return AppError(code="UNAUTHORIZED", message=message, status_code=401)


def forbidden(message: str = "forbidden") -> AppError:
    """创建无权限错误，统一映射为 403。"""
    return AppError(code="FORBIDDEN", message=message, status_code=403)


def not_found(message: str = "not found") -> AppError:
    """创建资源不可见或不存在错误，统一映射为 404。"""
    return AppError(code="NOT_FOUND", message=message, status_code=404)


def validation_error(message: str, data: Any | None = None) -> AppError:
    """创建业务参数校验错误，区别于框架层字段类型校验。"""
    return AppError(code="VALIDATION_ERROR", message=message, status_code=422, data=data)

