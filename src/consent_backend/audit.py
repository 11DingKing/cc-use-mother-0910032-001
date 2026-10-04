"""访问决定审计：只记录字段名与决定，结构上不承载敏感原值。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .domain import Purpose, Role


class AuditAction(str, Enum):
    READ_FIELD = "read_field"  # 敏感字段读取
    DATA_UPDATE = "data_update"  # 敏感字段写入
    ARCHIVE_CREATE = "archive_create"  # 历史服务留档
    ARCHIVE_READ = "archive_read"  # 留档读取
    EXPORT = "export"  # 导出请求
    CONSENT_GRANT = "consent_grant"  # 同意签署
    CONSENT_WITHDRAW = "consent_withdraw"  # 同意撤回
    CONSENT_EXPIRE = "consent_expire"  # 有效期届满
    GUARDIANSHIP_CHANGE = "guardianship_change"  # 监护关系变更
    DISCLOSURE_VIEW = "disclosure_view"  # 家长查看授权去向


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    RECORD = "record"  # 状态变更类记录，不涉及允许/拒绝


@dataclass(frozen=True)
class AuditEntry:
    """一条审计记录。

    类型层面只有字段名、决定与理由，没有任何承载原值的参数，
    保证“访问决定写入审计但不泄露原值”。
    """

    at: datetime
    actor_id: str
    actor_role: Role | None
    minor_id: str
    action: AuditAction
    decision: Decision
    reason: str
    field_names: tuple[str, ...] = ()
    purpose: Purpose | None = None
    consent_version: int | None = None
    seq: int = 0
