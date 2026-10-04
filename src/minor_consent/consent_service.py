"""同意生命周期服务：起草、提交、确认、撤回、续期、归档。

关键边界：
- 每个用途独立维护版本与有效期（用途化同意版本）；
- 撤回是单调的：已撤回字段只有监护人显式起草并确认新版本才能恢复，
  任何基于旧版本的并发写入都会被存储层拒绝；
- 归档后记录不可变，工作人员读取一律拒绝（留档边界）。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from . import policies
from .errors import ConsentStateError, GuardianshipError, NotFoundError
from .models import (
    Actor,
    ConsentStatus,
    ConsentVersion,
    Role,
    utcnow,
)
from .store import InMemoryStore

Clock = Callable[[], datetime]


class ConsentService:
    def __init__(self, store: InMemoryStore, clock: Clock = utcnow) -> None:
        self._store = store
        self._clock = clock

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def _require_current_guardian(self, actor: Actor, minor_id: str) -> None:
        guardianship = self._store.current_guardian(minor_id)
        if actor.role is not Role.GUARDIAN or guardianship.guardian_id != actor.actor_id:
            raise GuardianshipError("只有当前监护人可以执行该操作")

    def _active_status(self, minor_id: str) -> ConsentStatus:
        """确认类操作落库时的状态：已有进行中的服务关系则直接进入执行中。"""
        return (
            ConsentStatus.IN_SERVICE
            if self._store.has_active_service(minor_id)
            else ConsentStatus.CONFIRMED
        )

    def _audit(self, actor: Actor, minor_id: str, action: str, purpose: str,
               decision: str, reason: str, fields: tuple[str, ...] = (), detail: str = "") -> None:
        self._store.audit.record(
            timestamp=self._now(),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            minor_id=minor_id,
            action=action,
            decision=decision,
            reason=reason,
            purpose=purpose,
            fields=fields,
            detail=detail,
        )

    # ------------------------------------------------------------------
    # 起草 / 提交 / 确认
    # ------------------------------------------------------------------

    def create_draft(
        self,
        actor: Actor,
        minor_id: str,
        purpose: str,
        fields: set[str],
        valid_from: datetime,
        valid_until: datetime,
        note: str = "",
    ) -> ConsentVersion:
        """运营员起草新同意版本（首次或撤回后的重新授权都从这里开始）。"""
        if actor.role is not Role.CENTER_OPERATOR:
            raise ConsentStateError("只有文博中心运营员可以起草同意书")
        if not policies.known_purpose(purpose):
            raise NotFoundError(f"未知用途：{purpose}")
        unknown = fields - policies.PURPOSE_FIELDS[purpose]
        if unknown:
            raise ConsentStateError(f"用途 {purpose} 不包含字段：{sorted(unknown)}")
        if not fields:
            raise ConsentStateError("授权字段不能为空")
        if valid_until <= valid_from:
            raise ConsentStateError("有效期截止必须晚于起始时间")

        guardian = self._store.current_guardian(minor_id)
        current = self._store.find_consent(minor_id, purpose)
        if current is not None and current.status not in (
            ConsentStatus.ARCHIVED,
            ConsentStatus.DRAFT,
            ConsentStatus.PENDING,
        ) and current.effective_fields:
            raise ConsentStateError("当前仍有生效授权，请先撤回或等待到期再起草新版本")

        expected = current.version if current else 0
        now = self._now()

        def mutate(_: ConsentVersion | None) -> ConsentVersion:
            return ConsentVersion(
                minor_id=minor_id,
                purpose=purpose,
                version=expected + 1,
                status=ConsentStatus.DRAFT,
                guardian_id=guardian.guardian_id,
                granted_fields=frozenset(fields),
                withdrawn_fields=frozenset(),
                valid_from=valid_from,
                valid_until=valid_until,
                created_at=now,
                note=note,
            )

        version = self._store.mutate_consent(minor_id, purpose, expected, mutate)
        self._audit(actor, minor_id, "consent_draft", purpose, "recorded", "ok",
                    tuple(sorted(fields)))
        return version

    def submit(self, actor: Actor, minor_id: str, purpose: str, expected_version: int) -> ConsentVersion:
        """提交草稿给监护人核验：草拟 → 待核验。"""
        if actor.role is not Role.CENTER_OPERATOR:
            raise ConsentStateError("只有文博中心运营员可以提交同意书")

        def mutate(current: ConsentVersion | None) -> ConsentVersion:
            assert current is not None
            if current.status is not ConsentStatus.DRAFT:
                raise ConsentStateError("只有草拟状态可以提交")
            return ConsentVersion(
                **{**current.__dict__, "status": ConsentStatus.PENDING,
                   "version": current.version + 1, "created_at": self._now()}
            )

        version = self._store.mutate_consent(minor_id, purpose, expected_version, mutate)
        self._audit(actor, minor_id, "consent_submit", purpose, "recorded", "ok")
        return version

    def confirm(self, actor: Actor, minor_id: str, purpose: str, expected_version: int) -> ConsentVersion:
        """监护人确认：待核验 → 已确认/执行中。

        也用于监护关系变更后的重新核验（suspended 版本）。
        """
        self._require_current_guardian(actor, minor_id)

        def mutate(current: ConsentVersion | None) -> ConsentVersion:
            assert current is not None
            if current.status is not ConsentStatus.PENDING:
                raise ConsentStateError("只有待核验状态可以确认")
            return ConsentVersion(
                **{**current.__dict__,
                   "status": self._active_status(minor_id),
                   "guardian_id": actor.actor_id,
                   "suspended": False,
                   "version": current.version + 1,
                   "created_at": self._now()}
            )

        version = self._store.mutate_consent(minor_id, purpose, expected_version, mutate)
        self._audit(actor, minor_id, "consent_confirm", purpose, "recorded", "ok",
                    tuple(sorted(version.granted_fields)))
        return version

    # ------------------------------------------------------------------
    # 撤回（支持部分撤回）
    # ------------------------------------------------------------------

    def withdraw(
        self,
        actor: Actor,
        minor_id: str,
        purpose: str,
        fields: set[str] | None = None,
        expected_version: int | None = None,
        reason: str = "",
    ) -> ConsentVersion:
        """监护人撤回授权；``fields=None`` 表示撤回整个用途。

        部分撤回只封存指定字段，其余字段授权继续有效。
        撤回立即生效：工作人员对相关字段的读取从下一版本起一律拒绝。
        """
        self._require_current_guardian(actor, minor_id)
        current = self._store.find_consent(minor_id, purpose)
        if current is None:
            raise NotFoundError(f"同意记录不存在：{minor_id}/{purpose}")
        if expected_version is None:
            expected_version = current.version
        withdrawn_now_box: list[tuple[str, ...]] = []

        def mutate(latest: ConsentVersion | None) -> ConsentVersion:
            assert latest is not None
            if latest.status not in (ConsentStatus.CONFIRMED, ConsentStatus.IN_SERVICE):
                raise ConsentStateError("只有已确认或执行中的授权可以撤回")
            target = set(latest.effective_fields) if fields is None else set(fields)
            unknown = target - set(latest.granted_fields)
            if unknown:
                raise ConsentStateError(f"字段不在授权范围内：{sorted(unknown)}")
            if not target:
                raise ConsentStateError("撤回字段不能为空")
            withdrawn_now_box.append(tuple(sorted(target - set(latest.withdrawn_fields))))
            return ConsentVersion(
                **{**latest.__dict__,
                   "withdrawn_fields": frozenset(set(latest.withdrawn_fields) | target),
                   "version": latest.version + 1,
                   "created_at": self._now(),
                   "note": reason or latest.note}
            )

        version = self._store.mutate_consent(minor_id, purpose, expected_version, mutate)
        withdrawn_now = withdrawn_now_box[0] if withdrawn_now_box else ()
        self._audit(actor, minor_id, "consent_withdraw", purpose, "recorded", "ok",
                    withdrawn_now, detail=reason)
        return version

    # ------------------------------------------------------------------
    # 续期与归档
    # ------------------------------------------------------------------

    def renew(
        self,
        actor: Actor,
        minor_id: str,
        purpose: str,
        valid_until: datetime,
        expected_version: int,
    ) -> ConsentVersion:
        """运营员延长有效期；不能借此恢复已撤回字段。"""
        if actor.role is not Role.CENTER_OPERATOR:
            raise ConsentStateError("只有文博中心运营员可以续期")

        def mutate(current: ConsentVersion | None) -> ConsentVersion:
            assert current is not None
            if current.status not in (ConsentStatus.CONFIRMED, ConsentStatus.IN_SERVICE):
                raise ConsentStateError("只有生效中的授权可以续期")
            if valid_until <= current.valid_until:
                raise ConsentStateError("新有效期必须晚于原有效期")
            return ConsentVersion(
                **{**current.__dict__, "valid_until": valid_until,
                   "version": current.version + 1, "created_at": self._now()}
            )

        version = self._store.mutate_consent(minor_id, purpose, expected_version, mutate)
        self._audit(actor, minor_id, "consent_renew", purpose, "recorded", "ok")
        return version

    def archive(self, actor: Actor, minor_id: str, purpose: str, expected_version: int) -> ConsentVersion:
        """归档：服务结束后转入已归档，记录不可变、工作人员不可读。"""
        if actor.role is not Role.VENUE_MANAGER:
            raise ConsentStateError("只有场馆负责人可以归档")
        if self._store.has_active_service(minor_id):
            raise ConsentStateError("仍存在进行中的服务关系，不能归档")

        def mutate(current: ConsentVersion | None) -> ConsentVersion:
            assert current is not None
            if current.status is ConsentStatus.ARCHIVED:
                raise ConsentStateError("记录已归档")
            return ConsentVersion(
                **{**current.__dict__, "status": ConsentStatus.ARCHIVED,
                   "version": current.version + 1, "created_at": self._now()}
            )

        version = self._store.mutate_consent(minor_id, purpose, expected_version, mutate)
        self._audit(actor, minor_id, "consent_archive", purpose, "recorded", "ok")
        return version

    # ------------------------------------------------------------------
    # 监护关系变更触发的挂起（由 GuardianshipService 调用）
    # ------------------------------------------------------------------

    def activate_for_service(self, actor: Actor, minor_id: str) -> list[ConsentVersion]:
        """服务关系建立：已确认的同意自动进入执行中。"""
        activated: list[ConsentVersion] = []
        for current in self._store.consents_of_minor(minor_id):
            if current.status is not ConsentStatus.CONFIRMED or current.suspended:
                continue

            def mutate(latest: ConsentVersion | None) -> ConsentVersion:
                assert latest is not None
                return ConsentVersion(
                    **{**latest.__dict__,
                       "status": ConsentStatus.IN_SERVICE,
                       "version": latest.version + 1,
                       "created_at": self._now()}
                )

            version = self._store.mutate_consent(
                minor_id, current.purpose, current.version, mutate
            )
            activated.append(version)
            self._audit(actor, minor_id, "consent_activate", current.purpose,
                        "recorded", "service_started")
        return activated

    def suspend_for_guardianship_change(self, actor: Actor, minor_id: str, detail: str) -> list[ConsentVersion]:
        """监护关系变更：所有未归档同意回到待核验并挂起，等待新监护人核验。"""
        suspended: list[ConsentVersion] = []
        for current in self._store.consents_of_minor(minor_id):
            if current.status in (ConsentStatus.ARCHIVED, ConsentStatus.DRAFT):
                continue

            def mutate(latest: ConsentVersion | None) -> ConsentVersion:
                assert latest is not None
                return ConsentVersion(
                    **{**latest.__dict__,
                       "status": ConsentStatus.PENDING,
                       "suspended": True,
                       "version": latest.version + 1,
                       "created_at": self._now(),
                       "note": "监护关系变更，待新监护人核验"}
                )

            version = self._store.mutate_consent(
                minor_id, current.purpose, current.version, mutate
            )
            suspended.append(version)
            self._audit(actor, minor_id, "consent_suspend", current.purpose,
                        "recorded", "guardianship_changed", detail=detail)
        return suspended
