"""对外 API 门面：家长端与工作人员端的统一入口。

无框架依赖的纯 Python 门面，可直接挂载到任意 Web 框架；
所有变更类操作都要求 ``expected_version``（乐观并发），
并发更新不会恢复已撤销的权限——过期版本的写入一律被拒绝并留痕。
"""
from __future__ import annotations

from datetime import datetime

from . import policies
from .access_service import AccessService
from .consent_service import ConsentService
from .export_service import ExportService
from .guardianship_service import GuardianshipService
from .models import Actor, Assignment, Role, utcnow
from .store import InMemoryStore


class MinorConsentAPI:
    """未成年人隐私同意后端的门面。"""

    def __init__(self, store: InMemoryStore | None = None, clock=utcnow) -> None:
        self.store = store or InMemoryStore()
        self.consents = ConsentService(self.store, clock)
        self.access = AccessService(self.store, clock)
        self.guardianships = GuardianshipService(self.store, self.consents, clock)
        self.exports = ExportService(self.store, clock)
        self._clock = clock

    # ------------------------------------------------------------------
    # 建档与同意生命周期（运营员 / 监护人）
    # ------------------------------------------------------------------

    def register_guardian(self, actor: Actor, minor_id: str, guardian_id: str, relation: str) -> dict:
        g = self.guardianships.register(actor, minor_id, guardian_id, relation)
        return {"minor_id": g.minor_id, "guardian_id": g.guardian_id, "relation": g.relation}

    def put_sensitive_fields(self, actor: Actor, minor_id: str, purpose: str, values: dict[str, str]) -> dict:
        """录入敏感原值（运营员）；字段必须属于该用途。"""
        if actor.role is not Role.CENTER_OPERATOR:
            from .errors import AccessDeniedError
            raise AccessDeniedError("只有文博中心运营员可以录入敏感资料")
        unknown = set(values) - policies.PURPOSE_FIELDS.get(purpose, set())
        if unknown:
            from .errors import ConsentStateError
            raise ConsentStateError(f"用途 {purpose} 不包含字段：{sorted(unknown)}")
        self.store.put_profile_fields(minor_id, purpose, values)
        return {"stored_fields": sorted(values)}

    def create_draft(self, actor: Actor, minor_id: str, purpose: str, fields: set[str],
                     valid_from: datetime, valid_until: datetime, note: str = "") -> dict:
        return _version_dict(self.consents.create_draft(
            actor, minor_id, purpose, fields, valid_from, valid_until, note))

    def submit_consent(self, actor: Actor, minor_id: str, purpose: str, expected_version: int) -> dict:
        return _version_dict(self.consents.submit(actor, minor_id, purpose, expected_version))

    def confirm_consent(self, actor: Actor, minor_id: str, purpose: str, expected_version: int) -> dict:
        return _version_dict(self.consents.confirm(actor, minor_id, purpose, expected_version))

    def renew_consent(self, actor: Actor, minor_id: str, purpose: str,
                      valid_until: datetime, expected_version: int) -> dict:
        return _version_dict(self.consents.renew(
            actor, minor_id, purpose, valid_until, expected_version))

    # ------------------------------------------------------------------
    # 家长端
    # ------------------------------------------------------------------

    def parent_view_consents(self, actor: Actor, minor_id: str) -> dict:
        """家长查看各用途的授权现状（版本、有效期、可见角色）。"""
        self._require_guardian(actor, minor_id)
        return {
            "minor_id": minor_id,
            "purposes": {
                consent.purpose: {
                    **_version_dict(consent),
                    "visible_roles": {
                        name: sorted(r.value for r in policies.visible_roles(consent.purpose, name))
                        for name in sorted(consent.granted_fields)
                    },
                }
                for consent in self.store.consents_of_minor(minor_id)
            },
        }

    def parent_view_disclosure(self, actor: Actor, minor_id: str) -> dict:
        """家长查看授权去向：谁在何时因何任务访问/被拒了哪些字段。"""
        return {"minor_id": minor_id, "events": self.exports.disclosure(actor, minor_id)}

    def parent_export(self, actor: Actor, minor_id: str) -> dict:
        """家长导出全部资料与授权历史（数据主体权利）。"""
        return self.exports.guardian_export(actor, minor_id)

    def parent_withdraw(self, actor: Actor, minor_id: str, purpose: str,
                        fields: set[str] | None, expected_version: int,
                        reason: str = "") -> dict:
        """家长撤回授权（可部分撤回）；立即生效。"""
        return _version_dict(self.consents.withdraw(
            actor, minor_id, purpose, fields, expected_version, reason))

    # ------------------------------------------------------------------
    # 工作人员端
    # ------------------------------------------------------------------

    def read_sensitive_fields(self, actor: Actor, minor_id: str, purpose: str,
                              fields: set[str], task: str) -> dict:
        """工作人员按字段读取；三重校验 + 审计。"""
        return self.access.read_fields(actor, minor_id, purpose, fields, task)

    def compliance_read(self, actor: Actor, minor_id: str, purpose: str, fields: set[str]) -> dict:
        """已归档记录的合规读取（留档边界内唯一通道）。"""
        return self.access.compliance_read(actor, minor_id, purpose, fields)

    # ------------------------------------------------------------------
    # 服务关系 / 监护变更 / 归档 / 销毁
    # ------------------------------------------------------------------

    def start_service(self, actor: Actor, assignment_id: str, minor_id: str,
                      staff_id: str, service_id: str) -> dict:
        """登记服务关系（运营员排岗）。"""
        from .errors import AccessDeniedError
        if actor.role is not Role.CENTER_OPERATOR:
            raise AccessDeniedError("只有文博中心运营员可以登记服务关系")
        assignment = Assignment(
            assignment_id=assignment_id, minor_id=minor_id, staff_id=staff_id,
            service_id=service_id, started_at=self._clock(),
        )
        self.store.add_assignment(assignment)
        # 已确认的同意随服务开始自动进入“执行中”。
        self.consents.activate_for_service(actor, minor_id)
        return {"assignment_id": assignment_id, "active": True}

    def end_service(self, actor: Actor, assignment_id: str) -> dict:
        from .errors import AccessDeniedError
        if actor.role is not Role.CENTER_OPERATOR:
            raise AccessDeniedError("只有文博中心运营员可以结束服务关系")
        ended = self.store.end_assignment(assignment_id, self._clock())
        return {"assignment_id": ended.assignment_id, "active": ended.active}

    def transfer_guardianship(self, actor: Actor, minor_id: str, new_guardian_id: str,
                              relation: str, evidence: str) -> dict:
        g = self.guardianships.transfer(actor, minor_id, new_guardian_id, relation, evidence)
        return {"minor_id": g.minor_id, "guardian_id": g.guardian_id}

    def archive_consent(self, actor: Actor, minor_id: str, purpose: str, expected_version: int) -> dict:
        return _version_dict(self.consents.archive(actor, minor_id, purpose, expected_version))

    def purge_expired_archives(self, actor: Actor) -> dict:
        return {"purged": self.exports.purge_expired(actor)}

    # ------------------------------------------------------------------

    def _require_guardian(self, actor: Actor, minor_id: str) -> None:
        from .errors import GuardianshipError
        guardianship = self.store.current_guardian(minor_id)
        if actor.role is not Role.GUARDIAN or guardianship.guardian_id != actor.actor_id:
            raise GuardianshipError("只有当前监护人可以执行该操作")


def _version_dict(v) -> dict:
    return {
        "minor_id": v.minor_id,
        "purpose": v.purpose,
        "version": v.version,
        "status": v.status.value,
        "guardian_id": v.guardian_id,
        "granted_fields": sorted(v.granted_fields),
        "withdrawn_fields": sorted(v.withdrawn_fields),
        "effective_fields": sorted(v.effective_fields),
        "valid_from": v.valid_from.isoformat(),
        "valid_until": v.valid_until.isoformat(),
        "suspended": v.suspended,
    }
