"""测试共用的种子数据与后端工厂。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from consent_backend import ConsentBackend, Purpose, Role  # noqa: E402

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)
T2 = T0 + timedelta(days=2)
FAR_FUTURE = T0 + timedelta(days=365)

MINOR_ID = "m1"
GUARDIAN = "g-mother"
NEW_GUARDIAN = "g-father"
OPERATOR = "op-1"
MANAGER = "vm-1"
VOLUNTEER = "vol-1"
SERVICE_ID = "svc-1"

MINOR_DATA = {
    "emergency_contact_name": "张岚",
    "emergency_contact_relation": "母亲",
    "emergency_contact_phone": "13800001111",
    "health_allergies": "花生过敏",
    "health_medications": "无长期用药",
    "health_mobility_notes": "长时间站立需休息",
    "photo_release_scope": "仅限活动纪实",
    "photo_assets": "album/2026-spring/IMG_01.jpg",
}

SENSITIVE_VALUES = tuple(MINOR_DATA.values())


def make_backend(*, with_assignment: bool = True, grant_purposes=()) -> ConsentBackend:
    backend = ConsentBackend()
    backend.register_minor(MINOR_ID, "小舟", dict(MINOR_DATA))
    backend.guardianships.establish(MINOR_ID, GUARDIAN, actor_id="admin", now=T0)
    if with_assignment:
        backend.add_assignment(
            assignment_id="asg-op", staff_id=OPERATOR, role=Role.OPERATOR,
            minor_id=MINOR_ID, service_id=SERVICE_ID,
            purposes=set(Purpose), start=T0,
        )
        backend.add_assignment(
            assignment_id="asg-vm", staff_id=MANAGER, role=Role.VENUE_MANAGER,
            minor_id=MINOR_ID, service_id=SERVICE_ID,
            purposes=set(Purpose), start=T0,
        )
    for purpose in grant_purposes:
        backend.consents.grant(
            MINOR_ID, purpose, GUARDIAN,
            form_version="2026-v1", valid_until=FAR_FUTURE, now=T0,
        )
    return backend
