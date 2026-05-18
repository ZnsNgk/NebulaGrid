from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import get_api_router
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.responses import api_error


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例，并集中注册路由与异常处理器。"""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(get_api_router(), prefix="/api")
    register_exception_handlers(app)
    return app


def register_exception_handlers(app: FastAPI) -> None:
    """注册统一异常处理器，确保 API 返回格式稳定便于前端消费。"""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """把业务异常转换为统一错误响应，并保留请求 ID 方便排查。"""
        return api_error(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            request_id=get_request_id(request),
            data=exc.data,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """把参数校验错误压成统一格式，避免前端直接依赖 FastAPI 内部结构。"""
        return api_error(
            code="VALIDATION_ERROR",
            message="request validation failed",
            status_code=422,
            request_id=get_request_id(request),
            data={"errors": exc.errors()},
        )


def get_request_id(request: Request) -> str | None:
    """读取调用方传入的请求 ID，缺失时交给响应工具生成新的追踪标识。"""
    return request.headers.get("x-request-id")


app = create_app()
