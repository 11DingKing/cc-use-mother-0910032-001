"""未成年人隐私同意后端的异常类型。

所有异常都携带稳定的 ``reason`` 代码，审计日志与 API 层共用同一套
代码，保证“访问决定可审计、可复核”。
"""
from __future__ import annotations


class ConsentError(Exception):
    """后端业务异常基类。"""

    reason = "consent_error"

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message or self.reason)
        if reason is not None:
            self.reason = reason


class NotFoundError(ConsentError):
    """对象不存在。"""

    reason = "not_found"


class ConcurrencyError(ConsentError):
    """版本冲突：并发更新基于过期版本，被拒绝以防止恢复已撤销权限。"""

    reason = "version_conflict"


class ConsentStateError(ConsentError):
    """同意记录状态机不允许的迁移。"""

    reason = "illegal_state_transition"


class AccessDeniedError(ConsentError):
    """字段级访问被拒绝（决定已写入审计）。"""

    reason = "access_denied"


class GuardianshipError(ConsentError):
    """监护关系校验失败。"""

    reason = "guardianship_error"


class ArchivedError(ConsentError):
    """记录已归档，超出留档边界。"""

    reason = "archived"


class PurgedError(ConsentError):
    """留档期满，原值已销毁。"""

    reason = "purged"
