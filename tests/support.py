"""测试共享基座：可推进的时钟、角色与一套已授权的样例数据。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minor_consent import Actor, MinorConsentAPI, Role  # noqa: E402
from minor_consent import policies  # noqa: E402

BASE = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

MINOR = "minor-001"
OP = Actor("op-1", Role.CENTER_OPERATOR)
OP2 = Actor("op-2", Role.CENTER_OPERATOR)
MANAGER = Actor("mgr-1", Role.VENUE_MANAGER)
GUARDIAN = Actor("guardian-1", Role.GUARDIAN)
GUARDIAN2 = Actor("guardian-2", Role.GUARDIAN)
VOLUNTEER = Actor("vol-1", Role.VOLUNTEER)

SECRET_PHONE = "13800001111"
SECRET_ALLERGY = "花生过敏"
SECRET_PHOTO = "asset-2026-0001"


class Clock:
    def __init__(self, now: datetime = BASE) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


def grant(api: MinorConsentAPI, minor: str, purpose: str, fields: set[str],
          valid_from: datetime, valid_until: datetime) -> dict:
    """走完 草拟 → 待核验 → 已确认 全流程。"""
    draft = api.create_draft(OP, minor, purpose, fields, valid_from, valid_until)
    pending = api.submit_consent(OP, minor, purpose, draft["version"])
    return api.confirm_consent(GUARDIAN, minor, purpose, pending["version"])


def make_api(with_service: bool = True) -> tuple[MinorConsentAPI, Clock]:
    """构建一套含三个用途授权、两名工作人员在岗的样例环境。"""
    clock = Clock()
    api = MinorConsentAPI(clock=clock)
    api.register_guardian(OP, MINOR, GUARDIAN.actor_id, "母亲")
    api.put_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY, {
        "contact_name": "张三",
        "contact_phone": SECRET_PHONE,
        "contact_relation": "母子",
    })
    api.put_sensitive_fields(OP, MINOR, policies.PURPOSE_HEALTH, {
        "allergies": SECRET_ALLERGY,
        "medications": "无",
        "medical_conditions": "哮喘",
    })
    api.put_sensitive_fields(OP, MINOR, policies.PURPOSE_PHOTOS, {
        "photo_internal_archive": SECRET_PHOTO,
        "photo_public_display": SECRET_PHOTO,
        "photo_media_release": SECRET_PHOTO,
    })
    start = BASE - timedelta(days=1)
    end = BASE + timedelta(days=30)
    grant(api, MINOR, policies.PURPOSE_EMERGENCY,
          set(policies.PURPOSE_FIELDS[policies.PURPOSE_EMERGENCY]), start, end)
    grant(api, MINOR, policies.PURPOSE_HEALTH,
          set(policies.PURPOSE_FIELDS[policies.PURPOSE_HEALTH]), start, end)
    grant(api, MINOR, policies.PURPOSE_PHOTOS,
          set(policies.PURPOSE_FIELDS[policies.PURPOSE_PHOTOS]), start, end)
    if with_service:
        api.start_service(OP, "assign-op", MINOR, OP.actor_id, "svc-2026-10")
        api.start_service(OP, "assign-mgr", MINOR, MANAGER.actor_id, "svc-2026-10")
    return api, clock
