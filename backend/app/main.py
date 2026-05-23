import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import IntegrityError

from app.api.router import get_api_router
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.responses import api_error
from app.db.init_db import init_database
from app.services.file_executor import shutdown_file_operation_executor
from app.services.file_service import mark_interrupted_file_jobs
from app.services.user_service import ensure_existing_user_linux_accounts

logger = logging.getLogger(__name__)


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
    register_startup_tasks(app)
    return app


def register_startup_tasks(app: FastAPI) -> None:
    """启动时确保用户登录依赖的数据库表和默认管理员已存在。

    用户管理已从内存数据切换到 PostgreSQL，因此登录接口不再自带演示账号兜底。
    这里复用幂等的初始化逻辑，只创建缺失表和缺失默认数据，避免本地联调或新部署时忘记
    执行 scripts/init_db.py 导致 /api/auth/login 直接抛数据库异常。
    """

    @app.on_event("startup")
    def initialize_database_on_startup() -> None:
        """执行幂等数据库初始化，并补齐历史用户的文件根目录。"""
        init_database()
        mark_interrupted_file_jobs()
        try:
            ensure_existing_user_linux_accounts()
        except Exception:
            # Linux 子账户补齐依赖系统命令和 sudoers；部署遗漏时记录错误但不阻断 API 启动。
            logger.exception("failed to reconcile Linux accounts on startup")

    @app.on_event("shutdown")
    def shutdown_file_executor_on_stop() -> None:
        """关闭文件操作线程池，避免热重载后旧线程池继续接收新的文件任务。"""
        shutdown_file_operation_executor()


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

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        """把数据库唯一键/外键约束错误转成 400，避免节点管理等接口暴露 500。"""
        logger.warning("database integrity error: %s", exc)
        return api_error(
            code="CONSTRAINT_ERROR",
            message="request violates database constraints",
            status_code=400,
            request_id=get_request_id(request),
        )


def get_request_id(request: Request) -> str | None:
    """读取调用方传入的请求 ID，缺失时交给响应工具生成新的追踪标识。"""
    return request.headers.get("x-request-id")


app = create_app()
