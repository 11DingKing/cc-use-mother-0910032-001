"""同意、访问、留档、导出与家长查询的领域服务。

所有"读-判-写"序列都在 ``store.locked()`` 临界区内完成，保证并发下线性化。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from .audit import AuditAction, AuditEntry, Decision
from .domain import (
    FIELD_TO_PURPOSE,
    PURPOSE_LABELS,
    PURPOSE_POLICIES,
    ROLE_LABELS,
    ArchiveRecord,
    ConsentRecord,
    ConsentStatus,
    ExportRecord,
    GuardianshipRecord,
    MinorRecord,
    Purpose,
    PurposeSnapshot,
    Role,
)
from .errors import ConsentError, ConsentNotActive, NotFound, PermissionDenied
from .store import InMemoryStore

SYSTEM_ACTOR = "system"

RETENTION_NOTE = "已撤回或失效用途的历史数据仅按留档边界保留，不再用于业务读取。"

MIN_ARCHIVE_JUSTIFICATION = 8


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


class _BaseService:
    def __init__(self, store: InMemoryStore, clock: Callable[[], datetime] = default_clock) -> None:
        self._store = store
        self._clock = clock

    def _now(self, now: datetime | None) -> datetime:
        return now if now is not None else self._clock()

    def _require_minor(self, minor_id: str) -> MinorRecord:
        minor = self._store.get_minor(minor_id)
        if minor is None:
            raise NotFound(f"未成年人 {minor_id} 不存在")
        return minor

    def _require_current_guardian(self, minor_id: str, guardian_id: str, now: datetime) -> None:
        current = self._store.current_guardian_id(minor_id, now)
        if current is None:
            raise NotFound(f"未成年人 {minor_id} 尚未确立监护关系")
        if current != guardian_id:
            raise PermissionDenied("仅当前监护人可以执行该操作")

    def _audit(
        self,
        *,
        actor_id: str,
        actor_role: Role | None,
        minor_id: str,
        action: AuditAction,
        decision: Decision,
        reason: str,
        field_names: tuple[str, ...] = (),
        purpose: Purpose | None = None,
        consent_version: int | None = None,
        at: datetime | None = None,
    ) -> AuditEntry:
        return self._store.append_audit(
            AuditEntry(
                at=at or self._clock(),
                actor_id=actor_id,
                actor_role=actor_role,
                minor_id=minor_id,
                action=action,
                decision=decision,
                reason=reason,
                field_names=tuple(field_names),
                purpose=purpose,
                consent_version=consent_version,
            )
        )

    def _effective_head_locked(
        self, minor_id: str, purpose: Purpose, now: datetime
    ) -> ConsentRecord | None:
        """读取同意链头；有效期届满的版本惰性关闭并留痕。调用方须持有 store 锁。"""
        head = self._store.chain_head(minor_id, purpose)
        if (
            head is not None
            and head.status == ConsentStatus.ACTIVE
            and head.valid_until is not None
            and head.valid_until <= now
        ):
            head = self._store.append_consent(
                replace(
                    head,
                    status=ConsentStatus.EXPIRED,
                    closed_at=now,
                    close_reason="有效期届满",
                )
            )
            self._audit(
                actor_id=SYSTEM_ACTOR,
                actor_role=None,
                minor_id=minor_id,
                action=AuditAction.CONSENT_EXPIRE,
                decision=Decision.RECORD,
                reason="有效期届满",
                purpose=purpose,
                consent_version=head.version,
                at=now,
            )
        return head


class ConsentService(_BaseService):
    """按用途维护监护人同意的版本、有效期与状态。"""

    def grant(
        self,
        minor_id: str,
        purpose: Purpose,
        guardian_id: str,
        form_version: str,
        *,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        expected_head_seq: int | None = None,
        now: datetime | None = None,
    ) -> ConsentRecord:
        """签署新同意；同一用途再次签署会取代旧版本并递增版本号。"""
        now = self._now(now)
        valid_from = valid_from or now
        if valid_until is not None and valid_until <= valid_from:
            raise ConsentError("有效期截止必须晚于生效时间")
        with self._store.locked():
            self._require_minor(minor_id)
            self._require_current_guardian(minor_id, guardian_id, now)
            if expected_head_seq is not None:
                self._store.assert_head_seq(minor_id, purpose, expected_head_seq)
            head = self._effective_head_locked(minor_id, purpose, now)
            version = 1 if head is None else head.version + 1
            if head is not None and head.status == ConsentStatus.ACTIVE:
                self._store.append_consent(
                    replace(
                        head,
                        status=ConsentStatus.SUPERSEDED,
                        closed_at=now,
                        close_reason="被新版本同意书取代",
                    )
                )
            record = self._store.append_consent(
                ConsentRecord(
                    minor_id=minor_id,
                    purpose=purpose,
                    version=version,
                    form_version=form_version,
                    guardian_id=guardian_id,
                    status=ConsentStatus.ACTIVE,
                    granted_at=now,
                    valid_from=valid_from,
                    valid_until=valid_until,
                )
            )
            self._audit(
                actor_id=guardian_id,
                actor_role=Role.GUARDIAN,
                minor_id=minor_id,
                action=AuditAction.CONSENT_GRANT,
                decision=Decision.RECORD,
                reason=f"签署同意书模板 {form_version}",
                purpose=purpose,
                consent_version=version,
                at=now,
            )
            return record

    def withdraw(
        self,
        minor_id: str,
        purpose: Purpose,
        guardian_id: str,
        *,
        expected_head_seq: int | None = None,
        now: datetime | None = None,
    ) -> ConsentRecord:
        """撤回单个用途的同意（部分撤回），不影响其他用途。"""
        now = self._now(now)
        with self._store.locked():
            self._require_minor(minor_id)
            self._require_current_guardian(minor_id, guardian_id, now)
            if expected_head_seq is not None:
                self._store.assert_head_seq(minor_id, purpose, expected_head_seq)
            head = self._effective_head_locked(minor_id, purpose, now)
            if head is None or head.status != ConsentStatus.ACTIVE:
                raise ConsentNotActive(f"{PURPOSE_LABELS[purpose]}没有生效中的同意")
            record = self._store.append_consent(
                replace(
                    head,
                    status=ConsentStatus.WITHDRAWN,
                    closed_at=now,
                    close_reason="监护人撤回",
                )
            )
            self._audit(
                actor_id=guardian_id,
                actor_role=Role.GUARDIAN,
                minor_id=minor_id,
                action=AuditAction.CONSENT_WITHDRAW,
                decision=Decision.RECORD,
                reason="监护人撤回",
                purpose=purpose,
                consent_version=head.version,
                at=now,
            )
            return record

    def current_status(
        self, minor_id: str, purpose: Purpose, *, now: datetime | None = None
    ) -> ConsentRecord | None:
        with self._store.locked():
            return self._effective_head_locked(minor_id, purpose, self._now(now))


class GuardianshipService(_BaseService):
    """监护关系的确立与变更；变更即时作废旧监护人签署的全部生效同意。"""

    def establish(
        self,
        minor_id: str,
        guardian_id: str,
        *,
        actor_id: str,
        now: datetime | None = None,
    ) -> GuardianshipRecord:
        now = self._now(now)
        with self._store.locked():
            self._require_minor(minor_id)
            if self._store.current_guardianship(minor_id, now) is not None:
                raise ConsentError("已存在生效中的监护关系")
            record = self._store.append_guardianship(
                GuardianshipRecord(minor_id=minor_id, guardian_id=guardian_id, effective_from=now)
            )
            self._audit(
                actor_id=actor_id,
                actor_role=None,
                minor_id=minor_id,
                action=AuditAction.GUARDIANSHIP_CHANGE,
                decision=Decision.RECORD,
                reason=f"确立监护关系：{guardian_id}",
                at=now,
            )
            return record

    def change(
        self,
        minor_id: str,
        new_guardian_id: str,
        *,
        actor_id: str,
        reason: str = "监护关系变更",
        now: datetime | None = None,
    ) -> GuardianshipRecord:
        now = self._now(now)
        with self._store.locked():
            self._require_minor(minor_id)
            current = self._store.current_guardianship(minor_id, now)
            if current is None:
                raise NotFound(f"未成年人 {minor_id} 没有生效中的监护关系")
            if current.guardian_id == new_guardian_id:
                raise ConsentError("新监护人与当前监护人相同")
            self._store.close_current_guardianship(minor_id, now)
            record = self._store.append_guardianship(
                GuardianshipRecord(
                    minor_id=minor_id, guardian_id=new_guardian_id, effective_from=now
                )
            )
            # 旧监护人签署的生效同意全部作废，需新监护人重新签署。
            for purpose in Purpose:
                head = self._effective_head_locked(minor_id, purpose, now)
                if head is not None and head.status == ConsentStatus.ACTIVE:
                    self._store.append_consent(
                        replace(
                            head,
                            status=ConsentStatus.GUARDIANSHIP_INVALIDATED,
                            closed_at=now,
                            close_reason=reason,
                        )
                    )
            self._audit(
                actor_id=actor_id,
                actor_role=None,
                minor_id=minor_id,
                action=AuditAction.GUARDIANSHIP_CHANGE,
                decision=Decision.RECORD,
                reason=f"{reason}：{current.guardian_id} -> {new_guardian_id}",
                at=now,
            )
            return record


@dataclass(frozen=True)
class AccessResult:
    """一次敏感字段读取的决定；allowed 为 False 时 fields 必为空。"""

    purpose: Purpose
    allowed: bool
    reason: str
    fields: dict[str, str]
    consent_version: int | None


class AccessService(_BaseService):
    """字段级访问：同时校验服务关系与最小必要范围，并写入审计。"""

    def read_fields(
        self,
        actor_id: str,
        role: Role,
        minor_id: str,
        purpose: Purpose,
        *,
        requested_fields: tuple[str, ...] | list[str] | None = None,
        now: datetime | None = None,
    ) -> AccessResult:
        now = self._now(now)
        policy = PURPOSE_POLICIES[purpose]
        # 只承认用途内已知的字段名，防止调用方把原值伪装成字段名注入审计。
        requested = (
            None
            if requested_fields is None
            else set(requested_fields) & set(policy.fields)
        )
        with self._store.locked():
            minor = self._require_minor(minor_id)
            if role == Role.GUARDIAN:
                result = self._guardian_read(actor_id, minor, policy, requested, now)
            else:
                result = self._staff_read(actor_id, role, minor, policy, requested, now)
            self._audit(
                actor_id=actor_id,
                actor_role=role,
                minor_id=minor_id,
                action=AuditAction.READ_FIELD,
                decision=Decision.ALLOW if result.allowed else Decision.DENY,
                reason=result.reason,
                field_names=tuple(sorted(result.fields)) if result.allowed else tuple(sorted(requested or ())),
                purpose=purpose,
                consent_version=result.consent_version,
                at=now,
            )
            return result

    def _guardian_read(
        self,
        actor_id: str,
        minor: MinorRecord,
        policy,
        requested: set[str] | None,
        now: datetime,
    ) -> AccessResult:
        purpose = policy.purpose
        if self._store.current_guardian_id(minor.minor_id, now) != actor_id:
            return AccessResult(purpose, False, "not_current_guardian", {}, None)
        head = self._store.chain_head(minor.minor_id, purpose)
        names = set(policy.fields) if requested is None else requested
        fields = {n: minor.data[n] for n in policy.fields if n in names and n in minor.data}
        return AccessResult(
            purpose, True, "guardian_self_access", fields, head.version if head else None
        )

    def _staff_read(
        self,
        actor_id: str,
        role: Role,
        minor: MinorRecord,
        policy,
        requested: set[str] | None,
        now: datetime,
    ) -> AccessResult:
        purpose = policy.purpose
        if role not in policy.visible_roles:
            return AccessResult(purpose, False, "role_not_permitted", {}, None)
        assignment = self._store.active_assignment(actor_id, minor.minor_id, now)
        if assignment is None:
            return AccessResult(purpose, False, "no_active_assignment", {}, None)
        if purpose not in assignment.purposes:
            return AccessResult(purpose, False, "purpose_not_in_assignment_scope", {}, None)
        head = self._effective_head_locked(minor.minor_id, purpose, now)
        if head is None:
            return AccessResult(purpose, False, "consent_not_granted", {}, None)
        if head.status == ConsentStatus.WITHDRAWN:
            return AccessResult(purpose, False, "consent_withdrawn", {}, head.version)
        if head.status == ConsentStatus.EXPIRED:
            return AccessResult(purpose, False, "consent_expired", {}, head.version)
        if head.status == ConsentStatus.GUARDIANSHIP_INVALIDATED:
            return AccessResult(
                purpose, False, "consent_invalidated_guardianship", {}, head.version
            )
        if head.status != ConsentStatus.ACTIVE:
            return AccessResult(purpose, False, "consent_not_active", {}, head.version)
        if head.guardian_id != self._store.current_guardian_id(minor.minor_id, now):
            return AccessResult(
                purpose, False, "consent_invalidated_guardianship", {}, head.version
            )
        names = policy.minimal_for(role)
        if requested is not None:
            names &= requested
        fields = {n: minor.data[n] for n in policy.fields if n in names and n in minor.data}
        return AccessResult(purpose, True, "ok", fields, head.version)


class ArchiveService(_BaseService):
    """历史服务留档：冻结创建时有效同意下的字段；撤回不删档，读取需理由。"""

    def create_archive(
        self,
        actor_id: str,
        role: Role,
        minor_id: str,
        service_id: str,
        *,
        now: datetime | None = None,
    ) -> ArchiveRecord:
        now = self._now(now)
        with self._store.locked():
            minor = self._require_minor(minor_id)
            if role not in (Role.OPERATOR, Role.VENUE_MANAGER):
                self._audit(
                    actor_id=actor_id, actor_role=role, minor_id=minor_id,
                    action=AuditAction.ARCHIVE_CREATE, decision=Decision.DENY,
                    reason="role_not_permitted", at=now,
                )
                raise PermissionDenied("仅运营员与场馆负责人可以创建留档")
            if role == Role.OPERATOR and self._store.assignment_for_service(
                actor_id, minor_id, service_id
            ) is None:
                self._audit(
                    actor_id=actor_id, actor_role=role, minor_id=minor_id,
                    action=AuditAction.ARCHIVE_CREATE, decision=Decision.DENY,
                    reason="no_service_relationship", at=now,
                )
                raise PermissionDenied("运营员只能为自己参与的服务创建留档")
            snapshots: dict[Purpose, PurposeSnapshot] = {}
            for purpose in Purpose:
                head = self._effective_head_locked(minor_id, purpose, now)
                if head is not None and head.status == ConsentStatus.ACTIVE:
                    policy = PURPOSE_POLICIES[purpose]
                    values = {n: minor.data[n] for n in policy.fields if n in minor.data}
                    snapshots[purpose] = PurposeSnapshot(
                        purpose=purpose,
                        consent_version=head.version,
                        form_version=head.form_version,
                        field_names=tuple(values),
                        values=values,
                        frozen_at=now,
                    )
            archive = self._store.add_archive(
                ArchiveRecord(
                    archive_id=f"arc-{uuid4().hex[:12]}",
                    minor_id=minor_id,
                    service_id=service_id,
                    created_by=actor_id,
                    created_at=now,
                    retention_class="legal_hold",
                    snapshots=snapshots,
                )
            )
            self._audit(
                actor_id=actor_id, actor_role=role, minor_id=minor_id,
                action=AuditAction.ARCHIVE_CREATE, decision=Decision.RECORD,
                reason=f"服务 {service_id} 留档，含用途 {sorted(p.value for p in snapshots)}",
                field_names=tuple(sorted(n for s in snapshots.values() for n in s.field_names)),
                at=now,
            )
            return archive

    def read_archive(
        self,
        actor_id: str,
        role: Role,
        archive_id: str,
        purpose: Purpose,
        *,
        justification: str = "",
        now: datetime | None = None,
    ) -> PurposeSnapshot:
        """读取留档快照；撤回不影响留档，但读取受岗位与理由约束并审计。"""
        now = self._now(now)
        with self._store.locked():
            archive = self._store.get_archive(archive_id)
            if archive is None:
                raise NotFound(f"留档 {archive_id} 不存在")
            snapshot = archive.snapshots.get(purpose)
            deny_reason = None
            if role not in (Role.OPERATOR, Role.VENUE_MANAGER):
                deny_reason = "role_not_permitted"
            elif len(justification.strip()) < MIN_ARCHIVE_JUSTIFICATION:
                deny_reason = "justification_required"
            elif snapshot is None:
                deny_reason = "purpose_not_archived"
            self._audit(
                actor_id=actor_id, actor_role=role, minor_id=archive.minor_id,
                action=AuditAction.ARCHIVE_READ,
                decision=Decision.DENY if deny_reason else Decision.ALLOW,
                reason=deny_reason or f"留档核查：{justification.strip()}",
                field_names=() if deny_reason else snapshot.field_names,
                purpose=purpose,
                consent_version=None if deny_reason else snapshot.consent_version,
                at=now,
            )
            if deny_reason:
                raise PermissionDenied(f"留档读取被拒绝：{deny_reason}")
            return snapshot


@dataclass(frozen=True)
class ExportSection:
    purpose: Purpose
    consent_status: str
    consent_version: int | None
    form_version: str | None
    valid_until: datetime | None
    fields: dict[str, str]


@dataclass(frozen=True)
class ArchiveMetadata:
    archive_id: str
    service_id: str
    created_at: datetime
    purposes: tuple[Purpose, ...]
    retention_class: str


@dataclass(frozen=True)
class ExportPayload:
    """交给监护人本人的导出内容；系统侧只保存 ExportRecord 元数据。"""

    minor_id: str
    generated_at: datetime
    sections: tuple[ExportSection, ...]
    archives: tuple[ArchiveMetadata, ...]
    retention_note: str


class ExportService(_BaseService):
    """导出边界：仅当前监护人可导出；员工一律拒绝并留痕。"""

    def request_export(
        self,
        requester_id: str,
        role: Role,
        minor_id: str,
        *,
        now: datetime | None = None,
    ) -> ExportPayload:
        now = self._now(now)
        with self._store.locked():
            minor = self._require_minor(minor_id)
            if role != Role.GUARDIAN or self._store.current_guardian_id(minor_id, now) != requester_id:
                self._audit(
                    actor_id=requester_id, actor_role=role, minor_id=minor_id,
                    action=AuditAction.EXPORT, decision=Decision.DENY,
                    reason="export_guardian_only", at=now,
                )
                raise PermissionDenied("仅当前监护人可以导出未成年人资料")
            sections = []
            for purpose in Purpose:
                head = self._effective_head_locked(minor_id, purpose, now)
                policy = PURPOSE_POLICIES[purpose]
                values = {n: minor.data[n] for n in policy.fields if n in minor.data}
                sections.append(
                    ExportSection(
                        purpose=purpose,
                        consent_status=head.status.value if head else "not_granted",
                        consent_version=head.version if head else None,
                        form_version=head.form_version if head else None,
                        valid_until=head.valid_until if head else None,
                        fields=values,
                    )
                )
            archives = tuple(
                ArchiveMetadata(
                    archive_id=a.archive_id,
                    service_id=a.service_id,
                    created_at=a.created_at,
                    purposes=tuple(a.snapshots),
                    retention_class=a.retention_class,
                )
                for a in self._store.archives_for_minor(minor_id)
            )
            self._store.add_export(
                ExportRecord(
                    export_id=f"exp-{uuid4().hex[:12]}",
                    minor_id=minor_id,
                    requester_id=requester_id,
                    created_at=now,
                    section_fields={s.purpose: tuple(s.fields) for s in sections},
                    section_status={s.purpose: s.consent_status for s in sections},
                )
            )
            self._audit(
                actor_id=requester_id, actor_role=role, minor_id=minor_id,
                action=AuditAction.EXPORT, decision=Decision.ALLOW,
                reason="监护人导出本人子女资料",
                field_names=tuple(sorted(n for s in sections for n in s.fields)),
                at=now,
            )
            return ExportPayload(
                minor_id=minor_id,
                generated_at=now,
                sections=tuple(sections),
                archives=archives,
                retention_note=RETENTION_NOTE,
            )


@dataclass(frozen=True)
class ConsentStatusView:
    purpose: Purpose
    purpose_label: str
    status: str
    version: int | None
    form_version: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    granted_by: str | None
    visible_roles: tuple[str, ...]


@dataclass(frozen=True)
class AccessTraceEntry:
    at: datetime
    actor_id: str
    actor_role: str
    action: str
    decision: str
    reason: str
    field_names: tuple[str, ...]
    purpose: str | None
    consent_version: int | None


@dataclass(frozen=True)
class GuardianshipView:
    guardian_id: str
    effective_from: datetime
    effective_to: datetime | None


@dataclass(frozen=True)
class DisclosureView:
    """家长视角的授权去向：同意现状、谁在何时访问了哪些字段、导出与留档。"""

    minor_id: str
    guardian_id: str
    generated_at: datetime
    consents: tuple[ConsentStatusView, ...]
    access_trace: tuple[AccessTraceEntry, ...]
    exports: tuple[ExportRecord, ...]
    archives: tuple[ArchiveMetadata, ...]
    guardianship_history: tuple[GuardianshipView, ...]


class ParentPortal(_BaseService):
    """家长查询接口：仅当前监护人可见，查询本身也留痕。"""

    def disclosure_view(
        self, guardian_id: str, minor_id: str, *, now: datetime | None = None
    ) -> DisclosureView:
        now = self._now(now)
        with self._store.locked():
            self._require_minor(minor_id)
            if self._store.current_guardian_id(minor_id, now) != guardian_id:
                self._audit(
                    actor_id=guardian_id, actor_role=Role.GUARDIAN, minor_id=minor_id,
                    action=AuditAction.DISCLOSURE_VIEW, decision=Decision.DENY,
                    reason="not_current_guardian", at=now,
                )
                raise PermissionDenied("仅当前监护人可以查看授权去向")
            consents = tuple(self._consent_view(minor_id, purpose, now) for purpose in Purpose)
            trace = tuple(
                AccessTraceEntry(
                    at=e.at,
                    actor_id=e.actor_id,
                    actor_role=ROLE_LABELS.get(e.actor_role, "系统"),
                    action=e.action.value,
                    decision=e.decision.value,
                    reason=e.reason,
                    field_names=e.field_names,
                    purpose=e.purpose.value if e.purpose else None,
                    consent_version=e.consent_version,
                )
                for e in self._store.audit_entries(minor_id=minor_id)
            )
            view = DisclosureView(
                minor_id=minor_id,
                guardian_id=guardian_id,
                generated_at=now,
                consents=consents,
                access_trace=trace,
                exports=self._store.exports_for_minor(minor_id),
                archives=tuple(
                    ArchiveMetadata(
                        archive_id=a.archive_id,
                        service_id=a.service_id,
                        created_at=a.created_at,
                        purposes=tuple(a.snapshots),
                        retention_class=a.retention_class,
                    )
                    for a in self._store.archives_for_minor(minor_id)
                ),
                guardianship_history=tuple(
                    GuardianshipView(
                        guardian_id=g.guardian_id,
                        effective_from=g.effective_from,
                        effective_to=g.effective_to,
                    )
                    for g in self._store.guardianship_history(minor_id)
                ),
            )
            self._audit(
                actor_id=guardian_id, actor_role=Role.GUARDIAN, minor_id=minor_id,
                action=AuditAction.DISCLOSURE_VIEW, decision=Decision.ALLOW,
                reason="guardian_disclosure", at=now,
            )
            return view

    def _consent_view(self, minor_id: str, purpose: Purpose, now: datetime) -> ConsentStatusView:
        policy = PURPOSE_POLICIES[purpose]
        head = self._effective_head_locked(minor_id, purpose, now)
        return ConsentStatusView(
            purpose=purpose,
            purpose_label=PURPOSE_LABELS[purpose],
            status=head.status.value if head else "not_granted",
            version=head.version if head else None,
            form_version=head.form_version if head else None,
            valid_from=head.valid_from if head else None,
            valid_until=head.valid_until if head else None,
            granted_by=head.guardian_id if head else None,
            visible_roles=tuple(ROLE_LABELS[r] for r in sorted(policy.visible_roles, key=Role)),
        )


class DataService(_BaseService):
    """敏感字段写入：与读取同一边界——服务关系加生效同意，或当前监护人本人。"""

    def update_fields(
        self,
        actor_id: str,
        role: Role,
        minor_id: str,
        updates: dict[str, str],
        *,
        now: datetime | None = None,
    ) -> MinorRecord:
        now = self._now(now)
        if not updates:
            raise ConsentError("更新内容不能为空")
        unknown = sorted(set(updates) - set(FIELD_TO_PURPOSE))
        if unknown:
            raise ConsentError(f"未知敏感字段：{'、'.join(unknown)}")
        with self._store.locked():
            self._require_minor(minor_id)
            deny_reason = None
            if role == Role.GUARDIAN:
                if self._store.current_guardian_id(minor_id, now) != actor_id:
                    deny_reason = "not_current_guardian"
            else:
                if self._store.active_assignment(actor_id, minor_id, now) is None:
                    deny_reason = "no_active_assignment"
                else:
                    for purpose in {FIELD_TO_PURPOSE[name] for name in updates}:
                        head = self._effective_head_locked(minor_id, purpose, now)
                        if head is None or head.status != ConsentStatus.ACTIVE:
                            deny_reason = f"consent_not_active:{purpose.value}"
                            break
            self._audit(
                actor_id=actor_id, actor_role=role, minor_id=minor_id,
                action=AuditAction.DATA_UPDATE,
                decision=Decision.DENY if deny_reason else Decision.ALLOW,
                reason=deny_reason or "字段更新",
                field_names=tuple(sorted(updates)),
                at=now,
            )
            if deny_reason:
                raise PermissionDenied(f"敏感字段写入被拒绝：{deny_reason}")
            return self._store.update_minor_data(minor_id, updates)
