"""导出与留档销毁服务。

边界约定：
- 监护人导出走数据主体通道：可看到全部原值（含已撤回封存字段，标注 sealed）、
  完整同意版本史、访问决定审计（授权去向）与导出去向；
- 工作人员没有批量导出通道，只能经 AccessService 逐字段读取；
- 已归档记录超过留档期后销毁原值，仅保留审计与墓碑，导出中显示“已销毁”。
"""
from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import datetime, timedelta

from . import policies
from .errors import GuardianshipError
from .models import Actor, ConsentStatus, ExportRecord, Role, utcnow
from .store import InMemoryStore

Clock = Callable[[], datetime]


class ExportService:
    def __init__(self, store: InMemoryStore, clock: Clock = utcnow) -> None:
        self._store = store
        self._clock = clock
        self._export_seq = itertools.count(1)

    # ------------------------------------------------------------------
    # 监护人数据主体导出
    # ------------------------------------------------------------------

    def guardian_export(self, actor: Actor, minor_id: str) -> dict:
        """监护人导出未成年子女的全部资料与授权去向。"""
        guardianship = self._store.current_guardian(minor_id)
        if actor.role is not Role.GUARDIAN or guardianship.guardian_id != actor.actor_id:
            raise GuardianshipError("只有当前监护人可以导出")

        now = self._clock()
        profile = self._store.profile_purposes(minor_id)
        purposes: dict[str, dict] = {}
        for consent in self._store.consents_of_minor(minor_id):
            stored = profile.get(consent.purpose, {})
            purposes[consent.purpose] = {
                "status": consent.status.value,
                "version": consent.version,
                "valid_from": consent.valid_from.isoformat(),
                "valid_until": consent.valid_until.isoformat(),
                "granted_fields": sorted(consent.granted_fields),
                "withdrawn_fields": sorted(consent.withdrawn_fields),
                "suspended": consent.suspended,
                "values": {
                    name: {
                        # 已撤回字段对工作人员封存，但数据主体仍可查看，标注 sealed。
                        "value": stored.get(name),
                        "sealed": name in consent.withdrawn_fields
                        or consent.status is ConsentStatus.ARCHIVED,
                        "destroyed": name not in stored,
                    }
                    for name in sorted(consent.granted_fields)
                },
            }

        export_id = f"EXP-{minor_id}-{next(self._export_seq)}"
        self._store.add_export(ExportRecord(
            export_id=export_id,
            requester_id=actor.actor_id,
            requester_role=actor.role.value,
            minor_id=minor_id,
            requested_at=now,
            scope="guardian_full",
        ))
        self._store.audit.record(
            timestamp=now,
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            minor_id=minor_id,
            action="export",
            decision="allow",
            reason="ok",
            detail=f"export_id={export_id}; scope=guardian_full",
        )
        return {
            "export_id": export_id,
            "minor_id": minor_id,
            "generated_at": now.isoformat(),
            "guardian_id": actor.actor_id,
            "purposes": purposes,
            "consent_history": [
                {
                    "purpose": v.purpose,
                    "version": v.version,
                    "status": v.status.value,
                    "guardian_id": v.guardian_id,
                    "granted_fields": sorted(v.granted_fields),
                    "withdrawn_fields": sorted(v.withdrawn_fields),
                    "valid_from": v.valid_from.isoformat(),
                    "valid_until": v.valid_until.isoformat(),
                    "created_at": v.created_at.isoformat(),
                    "suspended": v.suspended,
                    "note": v.note,
                }
                for consent in self._store.consents_of_minor(minor_id)
                for v in self._store.consent_history(minor_id, consent.purpose)
            ],
            "access_disclosure": self.disclosure(actor, minor_id),
            "guardianship_history": [
                {
                    "guardian_id": g.guardian_id,
                    "relation": g.relation,
                    "since": g.since.isoformat(),
                }
                for g in self._store.guardian_history(minor_id)
            ],
        }

    def disclosure(self, actor: Actor, minor_id: str) -> list[dict]:
        """授权去向：该未成年人全部访问决定（不含任何原值）。"""
        guardianship = self._store.current_guardian(minor_id)
        if actor.role is not Role.GUARDIAN or guardianship.guardian_id != actor.actor_id:
            raise GuardianshipError("只有当前监护人可以查看授权去向")
        return [
            {
                "seq": entry.seq,
                "timestamp": entry.timestamp.isoformat(),
                "actor_id": entry.actor_id,
                "actor_role": entry.actor_role,
                "action": entry.action,
                "decision": entry.decision,
                "reason": entry.reason,
                "purpose": entry.purpose,
                "fields": list(entry.fields),
                "detail": entry.detail,
            }
            for entry in self._store.audit.for_minor(minor_id)
        ]

    # ------------------------------------------------------------------
    # 留档期满销毁
    # ------------------------------------------------------------------

    def purge_expired(self, actor: Actor) -> list[dict]:
        """销毁超过留档期的已归档记录原值；返回销毁清单（仅字段名）。"""
        if actor.role is not Role.VENUE_MANAGER:
            raise GuardianshipError("只有场馆负责人可以执行留档销毁")
        now = self._clock()
        purged: list[dict] = []
        for consent in self._store.all_consents():
            if consent.status is not ConsentStatus.ARCHIVED:
                continue
            deadline = consent.valid_until + timedelta(
                days=policies.RETENTION_DAYS_AFTER_ARCHIVE
            )
            if now <= deadline:
                continue
            wiped = self._store.wipe_profile_fields(consent.minor_id, consent.purpose)
            if not wiped:
                continue
            self._store.audit.record(
                timestamp=now,
                actor_id=actor.actor_id,
                actor_role=actor.role.value,
                minor_id=consent.minor_id,
                action="purge",
                decision="recorded",
                reason="retention_expired",
                purpose=consent.purpose,
                fields=tuple(wiped),
            )
            purged.append({
                "minor_id": consent.minor_id,
                "purpose": consent.purpose,
                "fields": wiped,
            })
        return purged
