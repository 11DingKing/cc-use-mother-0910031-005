"""业务错误类型，HTTP 层据此映射状态码。"""
from __future__ import annotations


class AppError(Exception):
    """所有可预期业务错误的基类。"""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class ValidationError(AppError):
    status = 422
    code = "validation_failed"


class NotFoundError(AppError):
    status = 404
    code = "not_found"


class ConflictError(AppError):
    status = 409
    code = "conflict"


class StaleStateError(ConflictError):
    """期间/单据状态已被其他事务推进（典型：并发确认落败方）。"""

    code = "stale_state"


class PermissionError(AppError):
    status = 403
    code = "forbidden"
