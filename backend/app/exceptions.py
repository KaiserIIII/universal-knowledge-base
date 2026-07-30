"""
企业级知识库 — 全局异常处理与标准化响应
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import uuid4

from fastapi import Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError


# ══════════════════════════════════════════════════════════════════
# 自定义异常
# ══════════════════════════════════════════════════════════════════
class AppException(Exception):
    """应用层统一异常基类"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail: dict | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}
        super().__init__(message)


class NotFoundError(AppException):
    """资源不存在 (404)"""

    def __init__(self, resource: str, identifier: str):
        super().__init__(
            message=f"{resource} not found: {str(identifier)}",
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"resource": resource, "identifier": identifier},
        )


class ConflictError(AppException):
    """资源冲突 (409) — 如重复上传"""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(
            message=message,
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        )


class ValidationError(AppException):
    """业务校验异常 (422)"""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(
            message=message,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=detail,
        )


class ServiceUnavailableError(AppException):
    """外部服务不可用 (503)"""

    def __init__(self, service: str, reason: str = ""):
        super().__init__(
            message=f"Service '{service}' is unavailable{f': {reason}' if reason else ''}",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"service": service, "reason": reason},
        )


# ══════════════════════════════════════════════════════════════════
# 全局异常处理器
# ══════════════════════════════════════════════════════════════════
async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """处理 AppException 及其子类"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": True,
            "code": exc.status_code,
            "message": exc.message,
            "detail": exc.detail,
            "request_id": request.state.request_id if hasattr(request.state, "request_id") else None,
        },
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """处理 Pydantic 校验异常"""
    errors = []
    for err in exc.errors():
        errors.append({
            "field": " → ".join(str(loc) for loc in err["loc"]),
            "message": err["msg"],
            "type": err["type"],
        })

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": True,
            "code": 422,
            "message": "Request validation failed",
            "detail": {"errors": errors},
        },
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底: 未预料的异常"""
    import logging
    logger = logging.getLogger("app")
    logger.exception(f"Unhandled exception: {exc}")

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": True,
            "code": 500,
            "message": "Internal server error",
            "detail": {"type": type(exc).__name__} if not isinstance(exc, AppException) else {},
        },
    )


# ══════════════════════════════════════════════════════════════════
# 请求 ID 中间件
# ══════════════════════════════════════════════════════════════════
from starlette.middleware.base import BaseHTTPMiddleware


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求注入唯一的 trace ID"""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid4()))
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
