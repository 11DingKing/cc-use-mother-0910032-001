"""线程安全的内存存储：同意链、监护关系、服务关系、留档、导出与审计。

并发纪律：
- 所有方法内部取同一把可重入锁，服务层用 ``locked()`` 把"读-判-写"
  包成临界区，保证线性化；
- 同意链只追加不可变记录，状态机保证已关闭版本永远不能恢复为生效，
  撤销只能被"新版本重新签署"覆盖，不能被并发更新复活；
- ``expected_head_seq`` 提供比较并交换，基于过期快照的写入会被拒绝。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from itertools import count
from typing import Iterator

from .audit import AuditEntry
from .domain import (
    TERMINAL_STATUSES,
    ArchiveRecord,
    Assignment,
    AssignmentStatus,
    ConsentRecord,
    ConsentStatus,
    ExportRecord,
    GuardianshipRecord,
    MinorRecord,
    Purpose,
)
from .errors import ConsentError, ConcurrencyConflict, InvalidTransition, NotFound


class InMemoryStore:
    """进程内存储；接口即并发边界，替换为数据库实现时语义不变。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = count(1)
        self._minors: dict[str, MinorRecord] = {}
        self._consent_chains: dict[tuple[str, Purpose], list[ConsentRecord]] = {}
        self._guardianships: dict[str, list[GuardianshipRecord]] = {}
        self._assignments: dict[str, Assignment] = {}
        self._archives: dict[str, ArchiveRecord] = {}
        self._exports: list[ExportRecord] = []
        self._audit: list[AuditEntry] = []

    @contextmanager
    def locked(self) -> Iterator["InMemoryStore"]:
        """把多次读写包进同一临界区（可重入）。"""
        with self._lock:
            yield self

    def _next_seq(self) -> int:
        return next(self._seq)

    # ---- 未成年人档案 ----

    def register_minor(self, record: MinorRecord) -> MinorRecord:
        with self._lock:
            if record.minor_id in self._minors:
                raise ConsentError(f"未成年人 {record.minor_id} 已存在")
            self._minors[record.minor_id] = record
            return record

    def get_minor(self, minor_id: str) -> MinorRecord | None:
        with self._lock:
            return self._minors.get(minor_id)

    def update_minor_data(self, minor_id: str, updates: dict[str, str]) -> MinorRecord:
        with self._lock:
            minor = self._minors.get(minor_id)
            if minor is None:
                raise NotFound(f"未成年人 {minor_id} 不存在")
            updated = replace(minor, data={**minor.data, **updates})
            self._minors[minor_id] = updated
            return updated

    # ---- 同意链 ----

    def chain_head(self, minor_id: str, purpose: Purpose) -> ConsentRecord | None:
        with self._lock:
            chain = self._consent_chains.get((minor_id, purpose))
            return chain[-1] if chain else None

    def consent_chain(self, minor_id: str, purpose: Purpose) -> tuple[ConsentRecord, ...]:
        with self._lock:
            return tuple(self._consent_chains.get((minor_id, purpose), ()))

    def assert_head_seq(self, minor_id: str, purpose: Purpose, expected: int) -> None:
        with self._lock:
            chain = self._consent_chains.get((minor_id, purpose)) or []
            current = chain[-1].seq if chain else 0
            if current != expected:
                raise ConcurrencyConflict(
                    f"同意链头已变化（期望 {expected}，实际 {current}），请重试"
                )

    def append_consent(
        self, record: ConsentRecord, *, expected_head_seq: int | None = None
    ) -> ConsentRecord:
        """追加一条同意记录；状态机拒绝任何让已关闭版本复活为生效的写入。"""
        with self._lock:
            key = (record.minor_id, record.purpose)
            chain = self._consent_chains.setdefault(key, [])
            head = chain[-1] if chain else None
            if expected_head_seq is not None:
                current = head.seq if head else 0
                if current != expected_head_seq:
                    raise ConcurrencyConflict(
                        f"同意链头已变化（期望 {expected_head_seq}，实际 {current}），请重试"
                    )
            self._validate_transition(head, record)
            stored = replace(record, seq=self._next_seq())
            chain.append(stored)
            return stored

    @staticmethod
    def _validate_transition(head: ConsentRecord | None, nxt: ConsentRecord) -> None:
        if head is None:
            if nxt.version != 1 or nxt.status != ConsentStatus.ACTIVE:
                raise InvalidTransition("首条同意记录必须是版本 1 的生效记录")
            return
        if nxt.version == head.version:
            # 关闭当前版本：仅允许从生效进入终态，终态不可再流转。
            if head.status != ConsentStatus.ACTIVE or nxt.status not in TERMINAL_STATUSES:
                raise InvalidTransition("已关闭的同意版本不能恢复为生效状态")
            return
        if nxt.version == head.version + 1:
            # 重新签署：必须基于已关闭的旧版本，且只能是新的生效版本。
            if nxt.status != ConsentStatus.ACTIVE or head.status not in TERMINAL_STATUSES:
                raise InvalidTransition("新版本同意只能在旧版本关闭后生效")
            return
        raise InvalidTransition("同意版本号必须连续递增")

    # ---- 监护关系 ----

    def append_guardianship(self, record: GuardianshipRecord) -> GuardianshipRecord:
        with self._lock:
            history = self._guardianships.setdefault(record.minor_id, [])
            if history and history[-1].effective_to is None:
                raise ConsentError("存在未结束的监护关系，请先变更")
            stored = replace(record, seq=self._next_seq())
            history.append(stored)
            return stored

    def close_current_guardianship(self, minor_id: str, at: datetime) -> GuardianshipRecord:
        with self._lock:
            history = self._guardianships.get(minor_id) or []
            if not history or history[-1].effective_to is not None:
                raise NotFound(f"未成年人 {minor_id} 没有生效中的监护关系")
            history[-1] = replace(history[-1], effective_to=at)
            return history[-1]

    def current_guardianship(self, minor_id: str, at: datetime) -> GuardianshipRecord | None:
        with self._lock:
            for record in reversed(self._guardianships.get(minor_id, ())):
                if record.effective_from <= at and (
                    record.effective_to is None or record.effective_to > at
                ):
                    return record
            return None

    def current_guardian_id(self, minor_id: str, at: datetime) -> str | None:
        record = self.current_guardianship(minor_id, at)
        return record.guardian_id if record else None

    def guardianship_history(self, minor_id: str) -> tuple[GuardianshipRecord, ...]:
        with self._lock:
            return tuple(self._guardianships.get(minor_id, ()))

    # ---- 服务关系 ----

    def put_assignment(self, assignment: Assignment) -> Assignment:
        with self._lock:
            if assignment.assignment_id in self._assignments:
                raise ConsentError(f"岗位分配 {assignment.assignment_id} 已存在")
            self._assignments[assignment.assignment_id] = assignment
            return assignment

    def end_assignment(self, assignment_id: str, at: datetime) -> Assignment:
        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if assignment is None:
                raise NotFound(f"岗位分配 {assignment_id} 不存在")
            ended = replace(assignment, status=AssignmentStatus.ENDED, end=at)
            self._assignments[assignment_id] = ended
            return ended

    def active_assignment(
        self, staff_id: str, minor_id: str, at: datetime
    ) -> Assignment | None:
        with self._lock:
            for assignment in self._assignments.values():
                if (
                    assignment.staff_id == staff_id
                    and assignment.minor_id == minor_id
                    and assignment.covers(at)
                ):
                    return assignment
            return None

    def assignment_for_service(
        self, staff_id: str, minor_id: str, service_id: str
    ) -> Assignment | None:
        with self._lock:
            for assignment in self._assignments.values():
                if (
                    assignment.staff_id == staff_id
                    and assignment.minor_id == minor_id
                    and assignment.service_id == service_id
                ):
                    return assignment
            return None

    # ---- 留档与导出 ----

    def add_archive(self, archive: ArchiveRecord) -> ArchiveRecord:
        with self._lock:
            if archive.archive_id in self._archives:
                raise ConsentError(f"留档 {archive.archive_id} 已存在")
            self._archives[archive.archive_id] = archive
            return archive

    def get_archive(self, archive_id: str) -> ArchiveRecord | None:
        with self._lock:
            return self._archives.get(archive_id)

    def archives_for_minor(self, minor_id: str) -> tuple[ArchiveRecord, ...]:
        with self._lock:
            return tuple(a for a in self._archives.values() if a.minor_id == minor_id)

    def add_export(self, record: ExportRecord) -> ExportRecord:
        with self._lock:
            self._exports.append(record)
            return record

    def exports_for_minor(self, minor_id: str) -> tuple[ExportRecord, ...]:
        with self._lock:
            return tuple(e for e in self._exports if e.minor_id == minor_id)

    # ---- 审计 ----

    def append_audit(self, entry: AuditEntry) -> AuditEntry:
        with self._lock:
            stored = replace(entry, seq=self._next_seq())
            self._audit.append(stored)
            return stored

    def audit_entries(self, minor_id: str | None = None) -> tuple[AuditEntry, ...]:
        with self._lock:
            if minor_id is None:
                return tuple(self._audit)
            return tuple(e for e in self._audit if e.minor_id == minor_id)
