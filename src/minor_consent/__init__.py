"""未成年人隐私同意后端。

按数据用途维护监护人同意版本、有效期与可见角色；
读取敏感字段时同时校验服务关系与最小必要范围；
访问决定写入审计且不泄露原值。
"""
from .access_service import AccessService
from .api import MinorConsentAPI
from .consent_service import ConsentService
from .errors import (
    AccessDeniedError,
    ArchivedError,
    ConcurrencyError,
    ConsentError,
    ConsentStateError,
    GuardianshipError,
    NotFoundError,
    PurgedError,
)
from .export_service import ExportService
from .guardianship_service import GuardianshipService
from .models import Actor, ConsentStatus, Role
from .store import InMemoryStore

__all__ = [
    "AccessDeniedError",
    "AccessService",
    "Actor",
    "ArchivedError",
    "ConcurrencyError",
    "ConsentError",
    "ConsentService",
    "ConsentStateError",
    "ConsentStatus",
    "ExportService",
    "GuardianshipError",
    "GuardianshipService",
    "InMemoryStore",
    "MinorConsentAPI",
    "NotFoundError",
    "PurgedError",
    "Role",
]
