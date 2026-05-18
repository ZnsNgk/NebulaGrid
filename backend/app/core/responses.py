from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi.responses import JSONResponse


def build_request_id(request_id: str | None = None) -> str:
    """生成或复用请求 ID，方便串联前端、Nginx 和后端日志。"""
    if request_id:
        return request_id
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{now}-{uuid4().hex[:8]}"


def api_success(
    data: Any | None = None,
    message: str = "success",
    request_id: str | None = None,
) -> dict[str, Any]:
    """构造成功响应体，固定 ok/code/message/data/request_id 字段。"""
    return {
        "ok": True,
        "code": "OK",
        "message": message,
        "data": data,
        "request_id": build_request_id(request_id),
    }


def api_error(
    code: str,
    message: str,
    status_code: int,
    request_id: str | None = None,
    data: Any | None = None,
) -> JSONResponse:
    """构造错误 JSONResponse，保持 HTTP 状态与业务错误码同时可见。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "code": code,
            "message": message,
            "data": data,
            "request_id": build_request_id(request_id),
        },
    )

