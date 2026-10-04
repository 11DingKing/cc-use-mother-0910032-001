"""未成年人隐私同意的领域模型：角色、用途策略与记录。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Role(str, Enum):
    """平台岗位角色，与领域契约中的 actors 对应。"""

    OPERATOR = "operator"  # 文博中心运营员
    VOLUNTEER = "volunteer"  # 志愿者
    GUARDIAN = "guardian"  # 监护人
    VENUE_MANAGER = "venue_manager"  # 场馆负责人


ROLE_LABELS = {
    Role.OPERATOR: "文博中心运营员",
    Role.VOLUNTEER: "志愿者",
    Role.GUARDIAN: "监护人",
    Role.VENUE_MANAGER: "场馆负责人",
}


class Purpose(str, Enum):
    """数据用途：每类敏感字段独立授权、独立撤回。"""

    EMERGENCY_CONTACT = "emergency_contact"  # 紧急联系人
    HEALTH_NOTES = "health_notes"  # 健康注意事项
    SERVICE_PHOTOS = "service_photos"  # 服务照片授权


PURPOSE_LABELS = {
    Purpose.EMERGENCY_CONTACT: "紧急联系人",
    Purpose.HEALTH_NOTES: "健康注意事项",
    Purpose.SERVICE_PHOTOS: "服务照片授权",
}


@dataclass(frozen=True)
class PurposePolicy:
    """单个用途的字段清单、可见角色与每角色最小必要字段。"""

    purpose: Purpose
    fields: tuple[str, ...]
    visible_roles: frozenset[Role]
    minimal_fields: dict[Role, frozenset[str]]

    def __post_init__(self) -> None:
        known = set(self.fields)
        for role, names in self.minimal_fields.items():
            if role not in self.visible_roles:
                raise ValueError(f"{role.value} 不在 {self.purpose.value} 的可见角色内")
            extra = set(names) - known
            if extra:
                raise ValueError(f"最小必要字段超出用途字段：{sorted(extra)}")

    def minimal_for(self, role: Role) -> frozenset[str]:
        return self.minimal_fields.get(role, frozenset())


PURPOSE_POLICIES = {
    Purpose.EMERGENCY_CONTACT: PurposePolicy(
        purpose=Purpose.EMERGENCY_CONTACT,
        fields=(
            "emergency_contact_name",
            "emergency_contact_relation",
            "emergency_contact_phone",
        ),
        visible_roles=frozenset({Role.OPERATOR, Role.VENUE_MANAGER}),
        minimal_fields={
            # 一线运营只需姓名与电话，亲属关系非必要。
            Role.OPERATOR: frozenset({"emergency_contact_name", "emergency_contact_phone"}),
            Role.VENUE_MANAGER: frozenset({
                "emergency_contact_name",
                "emergency_contact_relation",
                "emergency_contact_phone",
            }),
        },
    ),
    Purpose.HEALTH_NOTES: PurposePolicy(
        purpose=Purpose.HEALTH_NOTES,
        fields=("health_allergies", "health_medications", "health_mobility_notes"),
        visible_roles=frozenset({Role.OPERATOR, Role.VENUE_MANAGER}),
        minimal_fields={
            # 一线只需过敏信息用于现场防护，用药与行动说明限负责人。
            Role.OPERATOR: frozenset({"health_allergies"}),
            Role.VENUE_MANAGER: frozenset({
                "health_allergies",
                "health_medications",
                "health_mobility_notes",
            }),
        },
    ),
    Purpose.SERVICE_PHOTOS: PurposePolicy(
        purpose=Purpose.SERVICE_PHOTOS,
        fields=("photo_release_scope", "photo_assets"),
        visible_roles=frozenset({Role.OPERATOR}),
        minimal_fields={
            Role.OPERATOR: frozenset({"photo_release_scope", "photo_assets"}),
        },
    ),
}

FIELD_TO_PURPOSE: dict[str, Purpose] = {
    name: purpose for purpose, policy in PURPOSE_POLICIES.items() for name in policy.fields
}


class ConsentStatus(str, Enum):
    """同意版本的状态。"""

    ACTIVE = "active"  # 生效中
    WITHDRAWN = "withdrawn"  # 监护人撤回
    EXPIRED = "expired"  # 有效期届满
    SUPERSEDED = "superseded"  # 被新版本同意书取代
    GUARDIANSHIP_INVALIDATED = "guardianship_invalidated"  # 监护关系变更失效


TERMINAL_STATUSES = frozenset({
    ConsentStatus.WITHDRAWN,
    ConsentStatus.EXPIRED,
    ConsentStatus.SUPERSEDED,
    ConsentStatus.GUARDIANSHIP_INVALIDATED,
})


@dataclass(frozen=True)
class MinorRecord:
    """未成年人档案；data 为敏感字段原值，仅经访问服务按最小必要放出。"""

    minor_id: str
    display_name: str
    data: dict[str, str]


@dataclass(frozen=True)
class ConsentRecord:
    """同意链上的一条不可变记录；每条 (minor_id, purpose) 链只追加不修改。"""

    minor_id: str
    purpose: Purpose
    version: int
    form_version: str
    guardian_id: str
    status: ConsentStatus
    granted_at: datetime
    valid_from: datetime
    valid_until: datetime | None
    closed_at: datetime | None = None
    close_reason: str | None = None
    seq: int = 0


@dataclass(frozen=True)
class GuardianshipRecord:
    """监护关系的生效区间；effective_to 为 None 表示当前监护人。"""

    minor_id: str
    guardian_id: str
    effective_from: datetime
    effective_to: datetime | None = None
    seq: int = 0


class AssignmentStatus(str, Enum):
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True)
class Assignment:
    """服务关系：员工在某服务中对该未成年人的岗位与用途范围。"""

    assignment_id: str
    staff_id: str
    role: Role
    minor_id: str
    service_id: str
    purposes: frozenset[Purpose]
    start: datetime
    end: datetime | None
    status: AssignmentStatus = AssignmentStatus.ACTIVE

    def covers(self, at: datetime) -> bool:
        return (
            self.status == AssignmentStatus.ACTIVE
            and self.start <= at
            and (self.end is None or self.end > at)
        )


@dataclass(frozen=True)
class PurposeSnapshot:
    """留档时按当时有效同意冻结的单个用途快照。"""

    purpose: Purpose
    consent_version: int
    form_version: str
    field_names: tuple[str, ...]
    values: dict[str, str]
    frozen_at: datetime


@dataclass(frozen=True)
class ArchiveRecord:
    """历史服务留档：创建后不可变，撤回不删除，但读取受岗位与理由约束。"""

    archive_id: str
    minor_id: str
    service_id: str
    created_by: str
    created_at: datetime
    retention_class: str
    snapshots: dict[Purpose, PurposeSnapshot]


@dataclass(frozen=True)
class ExportRecord:
    """导出请求的元数据留痕；只记字段名与状态，不复制导出的原值。"""

    export_id: str
    minor_id: str
    requester_id: str
    created_at: datetime
    section_fields: dict[Purpose, tuple[str, ...]]
    section_status: dict[Purpose, str]
