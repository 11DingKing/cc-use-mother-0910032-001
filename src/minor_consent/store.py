"""线程安全存储层：同意版本 CAS、监护关系、服务关系与敏感原值。

并发设计：
- 所有写操作在单把可重入锁内完成，保证可串行化；
- 每次同意变更必须携带 ``expected_version``（乐观并发控制），
  基于过期版本的写入会被拒绝——已撤销的授权不会被并发更新“复活”；
- 同意记录只追加新版本，历史版本不可变，天然满足留档要求。
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from .audit import AuditLog
from .errors import ConcurrencyError, NotFoundError
from .models import Assignment, AuditEntry, ConsentVersion, ExportRecord, Guardianship

ConsentKey = tuple[str, str]  # (minor_id, purpose)


class InMemoryStore:
    """进程内存储；接口与未来替换为数据库实现保持一致。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._profiles: dict[str, dict[str, dict[str, str]]] = {}
        self._consents: dict[ConsentKey, ConsentVersion] = {}
        self._consent_history: dict[ConsentKey, list[ConsentVersion]] = {}
        self._guardians: dict[str, Guardianship] = {}
        self._guardian_history: dict[str, list[Guardianship]] = {}
        self._assignments: dict[str, Assignment] = {}
        self._exports: list[ExportRecord] = []
        self.audit = AuditLog()

    # ------------------------------------------------------------------
    # 敏感资料原值（只有访问服务在授权后读取；审计永远不触碰）
    # ------------------------------------------------------------------

    def put_profile_fields(self, minor_id: str, purpose: str, values: dict[str, str]) -> None:
        with self._lock:
            self._profiles.setdefault(minor_id, {}).setdefault(purpose, {}).update(values)

    def read_profile_fields(self, minor_id: str, purpose: str, fields: set[str]) -> dict[str, str]:
        with self._lock:
            stored = self._profiles.get(minor_id, {}).get(purpose, {})
            return {name: stored[name] for name in fields if name in stored}

    def profile_purposes(self, minor_id: str) -> dict[str, dict[str, str]]:
        """导出用：返回全部原值的深拷贝（仅限数据主体导出路径调用）。"""
        with self._lock:
            return {
                purpose: dict(fields)
                for purpose, fields in self._profiles.get(minor_id, {}).items()
            }

    def wipe_profile_fields(self, minor_id: str, purpose: str) -> list[str]:
        """留档期满销毁原值，返回被销毁的字段名（审计只记字段名）。"""
        with self._lock:
            fields = self._profiles.get(minor_id, {}).get(purpose, {})
            names = sorted(fields)
            fields.clear()
            return names

    # ------------------------------------------------------------------
    # 同意版本（CAS）
    # ------------------------------------------------------------------

    def get_consent(self, minor_id: str, purpose: str) -> ConsentVersion:
        with self._lock:
            try:
                return self._consents[(minor_id, purpose)]
            except KeyError:
                raise NotFoundError(f"同意记录不存在：{minor_id}/{purpose}") from None

    def find_consent(self, minor_id: str, purpose: str) -> ConsentVersion | None:
        with self._lock:
            return self._consents.get((minor_id, purpose))

    def consents_of_minor(self, minor_id: str) -> list[ConsentVersion]:
        with self._lock:
            return [v for (mid, _), v in self._consents.items() if mid == minor_id]

    def all_consents(self) -> list[ConsentVersion]:
        with self._lock:
            return list(self._consents.values())

    def consent_history(self, minor_id: str, purpose: str) -> list[ConsentVersion]:
        with self._lock:
            return list(self._consent_history.get((minor_id, purpose), []))

    def mutate_consent(
        self,
        minor_id: str,
        purpose: str,
        expected_version: int,
        mutate: Callable[[ConsentVersion | None], ConsentVersion],
    ) -> ConsentVersion:
        """乐观并发更新：版本不匹配即拒绝，绝不静默覆盖他人变更。

        ``mutate`` 接收当前版本（不存在时为 None），必须返回
        ``version = 当前版本 + 1`` 的新版本对象。
        """
        key = (minor_id, purpose)
        with self._lock:
            current = self._consents.get(key)
            current_version = current.version if current else 0
            if current_version != expected_version:
                raise ConcurrencyError(
                    f"版本冲突：期望 {expected_version}，当前 {current_version}"
                )
            new_version = mutate(current)
            if new_version.version != current_version + 1:
                raise ConcurrencyError("新版本号必须是当前版本号 + 1")
            self._consents[key] = new_version
            self._consent_history.setdefault(key, []).append(new_version)
            return new_version

    # ------------------------------------------------------------------
    # 监护关系
    # ------------------------------------------------------------------

    def set_guardian(self, guardianship: Guardianship) -> None:
        with self._lock:
            self._guardians[guardianship.minor_id] = guardianship
            self._guardian_history.setdefault(guardianship.minor_id, []).append(guardianship)

    def current_guardian(self, minor_id: str) -> Guardianship:
        with self._lock:
            try:
                return self._guardians[minor_id]
            except KeyError:
                raise NotFoundError(f"监护关系不存在：{minor_id}") from None

    def guardian_history(self, minor_id: str) -> list[Guardianship]:
        with self._lock:
            return list(self._guardian_history.get(minor_id, []))

    # ------------------------------------------------------------------
    # 服务关系
    # ------------------------------------------------------------------

    def add_assignment(self, assignment: Assignment) -> None:
        with self._lock:
            self._assignments[assignment.assignment_id] = assignment

    def end_assignment(self, assignment_id: str, ended_at) -> Assignment:
        with self._lock:
            current = self._assignments.get(assignment_id)
            if current is None:
                raise NotFoundError(f"服务关系不存在：{assignment_id}")
            ended = Assignment(
                assignment_id=current.assignment_id,
                minor_id=current.minor_id,
                staff_id=current.staff_id,
                service_id=current.service_id,
                started_at=current.started_at,
                ended_at=ended_at,
            )
            self._assignments[assignment_id] = ended
            return ended

    def has_active_relation(self, staff_id: str, minor_id: str) -> bool:
        """工作人员与未成年人之间是否存在进行中的服务关系。"""
        with self._lock:
            return any(
                a.staff_id == staff_id and a.minor_id == minor_id and a.active
                for a in self._assignments.values()
            )

    def has_active_service(self, minor_id: str) -> bool:
        with self._lock:
            return any(a.minor_id == minor_id and a.active for a in self._assignments.values())

    # ------------------------------------------------------------------
    # 导出留痕
    # ------------------------------------------------------------------

    def add_export(self, record: ExportRecord) -> None:
        with self._lock:
            self._exports.append(record)

    def exports_of_minor(self, minor_id: str) -> list[ExportRecord]:
        with self._lock:
            return [e for e in self._exports if e.minor_id == minor_id]

    # ------------------------------------------------------------------
    # 审计便捷入口
    # ------------------------------------------------------------------

    def audit_entries(self) -> list[AuditEntry]:
        return self.audit.entries()
