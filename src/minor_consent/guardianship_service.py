"""监护关系服务：登记与变更。

边界约定：监护关系一旦变更，
- 原监护人立即失去一切家长端权限；
- 该未成年人所有未归档的同意记录挂起为“待核验”，工作人员读取同步被拒绝；
- 新监护人逐项重新核验后才恢复对应用途的授权；
- 历史监护关系与历史同意版本全部留档，不可改写。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .consent_service import ConsentService
from .errors import GuardianshipError, NotFoundError
from .models import Actor, Guardianship, Role, utcnow
from .store import InMemoryStore

Clock = Callable[[], datetime]


class GuardianshipService:
    def __init__(self, store: InMemoryStore, consents: ConsentService, clock: Clock = utcnow) -> None:
        self._store = store
        self._consents = consents
        self._clock = clock

    def register(self, actor: Actor, minor_id: str, guardian_id: str, relation: str) -> Guardianship:
        """首次登记监护关系（运营员办理）。"""
        if actor.role is not Role.CENTER_OPERATOR:
            raise GuardianshipError("只有文博中心运营员可以登记监护关系")
        try:
            self._store.current_guardian(minor_id)
        except NotFoundError:
            pass
        else:
            raise GuardianshipError("监护关系已存在，请使用变更流程")
        guardianship = Guardianship(
            minor_id=minor_id, guardian_id=guardian_id,
            relation=relation, since=self._clock(),
        )
        self._store.set_guardian(guardianship)
        return guardianship

    def transfer(self, actor: Actor, minor_id: str, new_guardian_id: str,
                 relation: str, evidence: str) -> Guardianship:
        """变更监护关系：换绑监护人并挂起全部未归档同意。"""
        if actor.role is not Role.VENUE_MANAGER:
            raise GuardianshipError("只有场馆负责人可以办理监护关系变更")
        current = self._store.current_guardian(minor_id)
        if current.guardian_id == new_guardian_id:
            raise GuardianshipError("新旧监护人相同")
        guardianship = Guardianship(
            minor_id=minor_id, guardian_id=new_guardian_id,
            relation=relation, since=self._clock(),
        )
        self._store.set_guardian(guardianship)
        self._store.audit.record(
            timestamp=self._clock(),
            actor_id=actor.actor_id,
            actor_role=actor.role.value,
            minor_id=minor_id,
            action="guardianship_transfer",
            decision="recorded",
            reason="ok",
            detail=f"new_guardian={new_guardian_id}; evidence={evidence}",
        )
        # 挂起所有未归档同意，等待新监护人重新核验。
        self._consents.suspend_for_guardianship_change(actor, minor_id, detail=f"evidence={evidence}")
        return guardianship
