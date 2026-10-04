"""后端领域错误类型。"""
from __future__ import annotations


class ConsentError(Exception):
    """同意领域错误基类。"""


class NotFound(ConsentError):
    """请求的实体不存在。"""


class PermissionDenied(ConsentError):
    """当前身份无权执行该操作。"""


class ConsentNotActive(ConsentError):
    """目标用途没有处于生效状态的同意。"""


class ConcurrencyConflict(ConsentError):
    """基于过期快照的并发更新被拒绝，调用方需重试。"""


class InvalidTransition(ConsentError):
    """同意状态流转不合法。"""
