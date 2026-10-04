"""用途化策略表：字段清单、可见角色、任务最小必要范围与留档期限。

对应领域契约不变量“用途化同意版本”与“字段级最小权限”：
- 每个用途独立维护同意版本与有效期；
- 每个字段独立声明可见角色；
- 每次读取必须声明任务，且请求字段 ⊆ 任务允许字段（最小必要）。
"""
from __future__ import annotations

from .models import Role

# ---------------------------------------------------------------------------
# 用途与字段
# ---------------------------------------------------------------------------

PURPOSE_EMERGENCY = "emergency_contact"  # 紧急联系人
PURPOSE_HEALTH = "health_notes"  # 健康注意事项
PURPOSE_PHOTOS = "service_photos"  # 服务照片授权

PURPOSE_FIELDS: dict[str, frozenset[str]] = {
    PURPOSE_EMERGENCY: frozenset({"contact_name", "contact_phone", "contact_relation"}),
    PURPOSE_HEALTH: frozenset({"allergies", "medications", "medical_conditions"}),
    PURPOSE_PHOTOS: frozenset({"photo_internal_archive", "photo_public_display", "photo_media_release"}),
}

#: 字段级可见角色：无关岗位（如志愿者）默认不可见任何敏感字段。
FIELD_ROLES: dict[str, dict[str, frozenset[Role]]] = {
    PURPOSE_EMERGENCY: {
        "contact_name": frozenset({Role.CENTER_OPERATOR, Role.VENUE_MANAGER}),
        "contact_phone": frozenset({Role.CENTER_OPERATOR, Role.VENUE_MANAGER}),
        "contact_relation": frozenset({Role.CENTER_OPERATOR, Role.VENUE_MANAGER}),
    },
    PURPOSE_HEALTH: {
        "allergies": frozenset({Role.CENTER_OPERATOR, Role.VENUE_MANAGER}),
        "medications": frozenset({Role.VENUE_MANAGER}),
        "medical_conditions": frozenset({Role.VENUE_MANAGER}),
    },
    PURPOSE_PHOTOS: {
        "photo_internal_archive": frozenset({Role.CENTER_OPERATOR, Role.VENUE_MANAGER}),
        "photo_public_display": frozenset({Role.CENTER_OPERATOR}),
        "photo_media_release": frozenset({Role.CENTER_OPERATOR, Role.VENUE_MANAGER}),
    },
}

# ---------------------------------------------------------------------------
# 任务最小必要范围：(用途, 任务) → 允许读取的字段上限
# ---------------------------------------------------------------------------

TASK_INCIDENT = "incident_response"  # 突发事件处置
TASK_ROUTINE_CONTACT = "routine_contact"  # 日常联络
TASK_MEAL_PLANNING = "meal_planning"  # 餐饮安排
TASK_MEDICAL_SUPPORT = "medical_support"  # 医疗保障
TASK_NEWSLETTER = "newsletter"  # 活动简报
TASK_PRESS_RELEASE = "press_release"  # 媒体通稿
TASK_ARCHIVE_KEEP = "archive_keep"  # 档案留存
TASK_COMPLIANCE_AUDIT = "compliance_audit"  # 合规审计（仅场馆负责人、仅已归档记录）

TASK_SCOPES: dict[tuple[str, str], frozenset[str]] = {
    (PURPOSE_EMERGENCY, TASK_INCIDENT): frozenset({"contact_name", "contact_phone", "contact_relation"}),
    (PURPOSE_EMERGENCY, TASK_ROUTINE_CONTACT): frozenset({"contact_name", "contact_relation"}),
    (PURPOSE_HEALTH, TASK_MEAL_PLANNING): frozenset({"allergies"}),
    (PURPOSE_HEALTH, TASK_MEDICAL_SUPPORT): frozenset({"allergies", "medications", "medical_conditions"}),
    (PURPOSE_PHOTOS, TASK_NEWSLETTER): frozenset({"photo_public_display"}),
    (PURPOSE_PHOTOS, TASK_PRESS_RELEASE): frozenset({"photo_media_release"}),
    (PURPOSE_PHOTOS, TASK_ARCHIVE_KEEP): frozenset({"photo_internal_archive"}),
}

#: 已归档记录的留档天数；超过后原值必须销毁，仅保留审计与墓碑。
RETENTION_DAYS_AFTER_ARCHIVE = 365


def known_purpose(purpose: str) -> bool:
    return purpose in PURPOSE_FIELDS


def known_field(purpose: str, field_name: str) -> bool:
    return field_name in PURPOSE_FIELDS.get(purpose, frozenset())


def visible_roles(purpose: str, field_name: str) -> frozenset[Role]:
    return FIELD_ROLES.get(purpose, {}).get(field_name, frozenset())


def task_scope(purpose: str, task: str) -> frozenset[str]:
    """任务允许的最大字段集合；未知任务返回空集（即一律拒绝）。"""
    return TASK_SCOPES.get((purpose, task), frozenset())
