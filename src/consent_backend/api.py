"""后端门面：组合各领域服务，供 HTTP 层与测试使用。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable
from uuid import uuid4

from .audit import AuditEntry
from .domain import Assignment, MinorRecord, Purpose, Role
from .services import (
    AccessService,
    ArchiveService,
    ConsentService,
    DataService,
    ExportService,
    GuardianshipService,
    ParentPortal,
    default_clock,
)
from .store import InMemoryStore


class ConsentBackend:
    """未成年人隐私同意后端的统一入口。"""

    def __init__(
        self,
        store: InMemoryStore | None = None,
        clock: Callable[[], datetime] = default_clock,
    ) -> None:
        self.store = store or InMemoryStore()
        self.consents = ConsentService(self.store, clock)
        self.guardianships = GuardianshipService(self.store, clock)
        self.access = AccessService(self.store, clock)
        self.archives = ArchiveService(self.store, clock)
        self.exports = ExportService(self.store, clock)
        self.portal = ParentPortal(self.store, clock)
        self.data = DataService(self.store, clock)

    # ---- 管理侧便捷方法 ----

    def register_minor(
        self, minor_id: str, display_name: str, data: dict[str, str] | None = None
    ) -> MinorRecord:
        return self.store.register_minor(
            MinorRecord(minor_id=minor_id, display_name=display_name, data=dict(data or {}))
        )

    def add_assignment(
        self,
        *,
        staff_id: str,
        role: Role,
        minor_id: str,
        service_id: str,
        purposes: Iterable[Purpose],
        start: datetime | None = None,
        end: datetime | None = None,
        assignment_id: str | None = None,
    ) -> Assignment:
        return self.store.put_assignment(
            Assignment(
                assignment_id=assignment_id or f"asg-{uuid4().hex[:12]}",
                staff_id=staff_id,
                role=role,
                minor_id=minor_id,
                service_id=service_id,
                purposes=frozenset(purposes),
                start=start or datetime.now(timezone.utc),
                end=end,
            )
        )

    def end_assignment(self, assignment_id: str, *, now: datetime | None = None) -> Assignment:
        return self.store.end_assignment(
            assignment_id, now or datetime.now(timezone.utc)
        )

    def audit_trail(self, minor_id: str | None = None) -> tuple[AuditEntry, ...]:
        return self.store.audit_entries(minor_id=minor_id)
