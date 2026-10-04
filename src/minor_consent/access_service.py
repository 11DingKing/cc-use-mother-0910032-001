"""字段级访问授权：服务关系 + 可见角色 + 最小必要范围三重校验。

对应家长投诉的核心问题：旧同意书撤回后无关岗位仍能查看资料。
本服务保证：
- 任何读取先校验同意版本（状态、有效期、撤回集合）；
- 再校验调用者与未成年人之间存在进行中的服务关系；
- 再逐字段校验角色可见性与任务最小必要范围；
- 每一次决定（允许或拒绝）都写入审计，且审计不含原值。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from . import policies
from .errors import AccessDeniedError
from .models import Actor, ConsentVersion, Role, utcnow
from .store import InMemoryStore

Clock = Callable[[], datetime]

#: 拒绝理由代码（同时写入审计，供家长“授权去向”视图展示）
REASON_NO_CONSENT = "no_consent"
REASON_NOT_ACTIVE = "consent_not_active"
REASON_SUSPENDED = "consent_suspended"
REASON_EXPIRED = "consent_expired"
REASON_FIELD_WITHDRAWN = "field_withdrawn"
REASON_FIELD_NOT_GRANTED = "field_not_granted"
REASON_ROLE_NOT_VISIBLE = "role_not_visible"
REASON_NO_SERVICE_RELATION = "no_service_relation"
REASON_EXCEEDS_TASK_SCOPE = "exceeds_minimum_necessary"
REASON_ARCHIVED = "archived"


class AccessService:
    def __init__(self, store: InMemoryStore, clock: Clock = utcnow) -> None:
        self._store = store
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock()

    def _deny(self, actor: Actor, minor_id: str, purpose: str,
              fields: set[str], task: str, reason: str) -> AccessDeniedError:
        self._store.audit.record(
            timestamp=self._now(),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            minor_id=minor_id,
            action="field_read",
            decision="deny",
            reason=reason,
            purpose=purpose,
            fields=tuple(sorted(fields)),
            detail=f"task={task}",
        )
        return AccessDeniedError(f"访问被拒绝：{reason}", reason=reason)

    def read_fields(
        self,
        actor: Actor,
        minor_id: str,
        purpose: str,
        fields: set[str],
        task: str,
    ) -> dict[str, str]:
        """按字段读取敏感资料；任一校验失败即拒绝并审计。"""
        requested = set(fields)
        if not requested:
            raise self._deny(actor, minor_id, purpose, requested, task, "empty_request")
        if actor.role in (Role.GUARDIAN, Role.VOLUNTEER):
            # 监护人请走数据主体导出接口；志愿者不属于任何敏感字段的可见角色。
            raise self._deny(actor, minor_id, purpose, requested, task, REASON_ROLE_NOT_VISIBLE)
        if not policies.known_purpose(purpose) or any(
            not policies.known_field(purpose, f) for f in requested
        ):
            raise self._deny(actor, minor_id, purpose, requested, task, REASON_FIELD_NOT_GRANTED)

        consent = self._store.find_consent(minor_id, purpose)
        if consent is None:
            raise self._deny(actor, minor_id, purpose, requested, task, REASON_NO_CONSENT)
        self._check_consent(actor, consent, requested, task)

        # 服务关系：无进行中服务关联的岗位不得查看。
        if not self._store.has_active_relation(actor.actor_id, minor_id):
            raise self._deny(actor, minor_id, purpose, requested, task, REASON_NO_SERVICE_RELATION)

        # 字段级可见角色。
        for name in sorted(requested):
            if actor.role not in policies.visible_roles(purpose, name):
                raise self._deny(actor, minor_id, purpose, {name}, task, REASON_ROLE_NOT_VISIBLE)

        # 最小必要：请求字段不得超出任务允许范围。
        if not requested <= policies.task_scope(purpose, task):
            raise self._deny(actor, minor_id, purpose, requested, task, REASON_EXCEEDS_TASK_SCOPE)

        values = self._store.read_profile_fields(minor_id, purpose, requested)
        self._store.audit.record(
            timestamp=self._now(),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            minor_id=minor_id,
            action="field_read",
            decision="allow",
            reason="ok",
            purpose=purpose,
            fields=tuple(sorted(requested)),
            detail=f"task={task}",
        )
        return values

    def compliance_read(
        self,
        actor: Actor,
        minor_id: str,
        purpose: str,
        fields: set[str],
    ) -> dict[str, str]:
        """留档边界内唯一的归档读取通道：场馆负责人 + 合规审计任务。

        仅用于法定留档期内的合规核查；每次读取单独审计。
        """
        requested = set(fields)
        if actor.role is not Role.VENUE_MANAGER:
            raise self._deny(actor, minor_id, purpose, requested,
                             policies.TASK_COMPLIANCE_AUDIT, REASON_ROLE_NOT_VISIBLE)
        consent = self._store.find_consent(minor_id, purpose)
        if consent is None:
            raise self._deny(actor, minor_id, purpose, requested,
                             policies.TASK_COMPLIANCE_AUDIT, REASON_NO_CONSENT)
        from .models import ConsentStatus

        if consent.status is not ConsentStatus.ARCHIVED:
            raise self._deny(actor, minor_id, purpose, requested,
                             policies.TASK_COMPLIANCE_AUDIT, "not_archived")
        unknown = requested - set(consent.granted_fields)
        if unknown:
            raise self._deny(actor, minor_id, purpose, requested,
                             policies.TASK_COMPLIANCE_AUDIT, REASON_FIELD_NOT_GRANTED)
        values = self._store.read_profile_fields(minor_id, purpose, requested)
        if len(values) < len(requested):
            # 留档期满原值已销毁，只能读到墓碑。
            raise self._deny(actor, minor_id, purpose, requested,
                             policies.TASK_COMPLIANCE_AUDIT, "purged")
        self._store.audit.record(
            timestamp=self._now(),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            minor_id=minor_id,
            action="compliance_read",
            decision="allow",
            reason="ok",
            purpose=purpose,
            fields=tuple(sorted(requested)),
            detail=f"task={policies.TASK_COMPLIANCE_AUDIT}",
        )
        return values

    # ------------------------------------------------------------------

    def _check_consent(self, actor: Actor, consent: ConsentVersion,
                       requested: set[str], task: str) -> None:
        from .models import READABLE_STATUSES, ConsentStatus

        if consent.status is ConsentStatus.ARCHIVED:
            raise self._deny(actor, consent.minor_id, consent.purpose,
                             requested, task, REASON_ARCHIVED)
        if consent.status not in READABLE_STATUSES:
            raise self._deny(actor, consent.minor_id, consent.purpose,
                             requested, task, REASON_NOT_ACTIVE)
        if consent.suspended:
            raise self._deny(actor, consent.minor_id, consent.purpose,
                             requested, task, REASON_SUSPENDED)
        if not consent.is_within_validity(self._now()):
            raise self._deny(actor, consent.minor_id, consent.purpose,
                             requested, task, REASON_EXPIRED)
        withdrawn = requested & set(consent.withdrawn_fields)
        if withdrawn:
            raise self._deny(actor, consent.minor_id, consent.purpose,
                             withdrawn, task, REASON_FIELD_WITHDRAWN)
        if not requested <= set(consent.granted_fields):
            raise self._deny(actor, consent.minor_id, consent.purpose,
                             requested, task, REASON_FIELD_NOT_GRANTED)
