"""领域模型：角色、状态、同意版本、监护关系、服务关系与审计条目。

状态机沿用领域契约 ``domain/contract.json`` 中的五个状态：
草拟 → 待核验 → 已确认 → 执行中 → 已归档。
撤回不作为状态，而是叠加在版本上的单调标记，保证“撤销不可被并发更新恢复”。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Role(str, Enum):
    """领域契约中的四类角色。"""

    CENTER_OPERATOR = "文博中心运营员"
    VOLUNTEER = "志愿者"
    GUARDIAN = "监护人"
    VENUE_MANAGER = "场馆负责人"


class ConsentStatus(str, Enum):
    """同意记录生命周期（与领域契约 states 一一对应）。"""

    DRAFT = "草拟"
    PENDING = "待核验"
    CONFIRMED = "已确认"
    IN_SERVICE = "执行中"
    ARCHIVED = "已归档"


#: 允许工作人员按字段读取的状态；其余状态一律拒绝。
READABLE_STATUSES = frozenset({ConsentStatus.CONFIRMED, ConsentStatus.IN_SERVICE})


@dataclass(frozen=True)
class Actor:
    """一次操作的调用者身份（认证由部署侧完成，此处只做授权）。"""

    actor_id: str
    role: Role


@dataclass(frozen=True)
class ConsentVersion:
    """某一（未成年人, 用途）的同意记录的一个不可变版本。

    - ``granted_fields``：本版本授权的字段集合（按用途维护）。
    - ``withdrawn_fields``：截至本版本累计撤回的字段；撤回单调递增，
      只有监护人显式起草并确认新版本才能重新授权。
    - ``suspended``：监护关系变更后挂起，等待新监护人重新核验。
    """

    minor_id: str
    purpose: str
    version: int
    status: ConsentStatus
    guardian_id: str
    granted_fields: frozenset[str]
    withdrawn_fields: frozenset[str]
    valid_from: datetime
    valid_until: datetime
    created_at: datetime
    suspended: bool = False
    note: str = ""

    @property
    def effective_fields(self) -> frozenset[str]:
        """当前实际可用的字段 = 授权 − 已撤回。"""
        return self.granted_fields - self.withdrawn_fields

    def is_within_validity(self, now: datetime) -> bool:
        return self.valid_from <= now <= self.valid_until


@dataclass(frozen=True)
class Guardianship:
    """当前监护关系；历史关系由存储层另行留档。"""

    minor_id: str
    guardian_id: str
    relation: str
    since: datetime


@dataclass(frozen=True)
class Assignment:
    """服务关系：工作人员与未成年人在某次服务活动中的在岗关联。"""

    assignment_id: str
    minor_id: str
    staff_id: str
    service_id: str
    started_at: datetime
    ended_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.ended_at is None


@dataclass(frozen=True)
class AuditEntry:
    """访问决定审计条目。

    只记录“谁在何时因何任务访问了哪些字段、决定与理由”，
    绝不记录字段原值；通过哈希链防篡改。
    """

    seq: int
    timestamp: datetime
    actor_id: str
    actor_role: str
    minor_id: str
    action: str
    decision: str
    reason: str
    purpose: str = ""
    fields: tuple[str, ...] = ()
    detail: str = ""
    prev_hash: str = ""
    entry_hash: str = ""

    def payload(self) -> str:
        """用于哈希链的规范化载荷（不含任何敏感原值）。"""
        return json.dumps(
            {
                "seq": self.seq,
                "timestamp": self.timestamp.isoformat(),
                "actor_id": self.actor_id,
                "actor_role": self.actor_role,
                "minor_id": self.minor_id,
                "action": self.action,
                "decision": self.decision,
                "reason": self.reason,
                "purpose": self.purpose,
                "fields": list(self.fields),
                "detail": self.detail,
                "prev_hash": self.prev_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def with_hash(self, prev_hash: str) -> "AuditEntry":
        draft = AuditEntry(
            seq=self.seq,
            timestamp=self.timestamp,
            actor_id=self.actor_id,
            actor_role=self.actor_role,
            minor_id=self.minor_id,
            action=self.action,
            decision=self.decision,
            reason=self.reason,
            purpose=self.purpose,
            fields=self.fields,
            detail=self.detail,
            prev_hash=prev_hash,
        )
        digest = hashlib.sha256(draft.payload().encode("utf-8")).hexdigest()
        return AuditEntry(
            seq=draft.seq,
            timestamp=draft.timestamp,
            actor_id=draft.actor_id,
            actor_role=draft.actor_role,
            minor_id=draft.minor_id,
            action=draft.action,
            decision=draft.decision,
            reason=draft.reason,
            purpose=draft.purpose,
            fields=draft.fields,
            detail=draft.detail,
            prev_hash=prev_hash,
            entry_hash=digest,
        )


@dataclass(frozen=True)
class ExportRecord:
    """导出请求留痕。"""

    export_id: str
    requester_id: str
    requester_role: str
    minor_id: str
    requested_at: datetime
    scope: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_minor_profile() -> dict[str, dict[str, str]]:
    """空的未成年人敏感资料结构：用途 → 字段 → 原值（仅存于存储层）。"""
    return {}
