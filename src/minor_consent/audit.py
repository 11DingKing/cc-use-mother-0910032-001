"""访问决定审计：只追加、哈希链防篡改、绝不记录字段原值。

对应领域契约不变量“访问决定审计”。每条记录包含决定（allow/deny）、
理由代码、用途与字段名；任何敏感原值都不会进入日志。
"""
from __future__ import annotations

import threading
from datetime import datetime

from .models import AuditEntry

GENESIS_HASH = "0" * 64


class AuditLog:
    """线程安全的只追加审计日志。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[AuditEntry] = []

    def record(
        self,
        *,
        timestamp: datetime,
        actor_id: str,
        actor_role: str,
        minor_id: str,
        action: str,
        decision: str,
        reason: str,
        purpose: str = "",
        fields: tuple[str, ...] = (),
        detail: str = "",
    ) -> AuditEntry:
        """追加一条审计记录（调用方负责不传入任何敏感原值）。"""
        with self._lock:
            prev_hash = self._entries[-1].entry_hash if self._entries else GENESIS_HASH
            entry = AuditEntry(
                seq=len(self._entries) + 1,
                timestamp=timestamp,
                actor_id=actor_id,
                actor_role=actor_role,
                minor_id=minor_id,
                action=action,
                decision=decision,
                reason=reason,
                purpose=purpose,
                fields=tuple(sorted(fields)),
                detail=detail,
            ).with_hash(prev_hash)
            self._entries.append(entry)
            return entry

    def entries(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)

    def for_minor(self, minor_id: str) -> list[AuditEntry]:
        """某未成年人的全部审计记录（家长“授权去向”视图的数据源）。"""
        return [e for e in self.entries() if e.minor_id == minor_id]

    def verify_chain(self) -> bool:
        """校验哈希链完整性。"""
        prev = GENESIS_HASH
        for entry in self.entries():
            if entry.prev_hash != prev:
                return False
            if entry.with_hash(prev).entry_hash != entry.entry_hash:
                return False
            prev = entry.entry_hash
        return True
