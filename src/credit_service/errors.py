"""领域异常。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务规则错误的基类。"""

    code = "domain_error"


class ValidationError(DomainError):
    code = "validation_error"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    """状态冲突或并发竞争（含乐观锁失败、重复确认）。"""

    code = "conflict"
