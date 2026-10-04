"""未成年人隐私同意后端：用途化同意、最小必要访问、留档与审计。"""
from .api import ConsentBackend
from .audit import AuditAction, AuditEntry, Decision
from .domain import (
    FIELD_TO_PURPOSE,
    PURPOSE_LABELS,
    PURPOSE_POLICIES,
    ROLE_LABELS,
    ConsentStatus,
    Purpose,
    Role,
)
from .errors import (
    ConcurrencyConflict,
    ConsentError,
    ConsentNotActive,
    InvalidTransition,
    NotFound,
    PermissionDenied,
)
from .services import (
    AccessResult,
    DisclosureView,
    ExportPayload,
    RETENTION_NOTE,
)
from .store import InMemoryStore

__all__ = [
    "AccessResult",
    "AuditAction",
    "AuditEntry",
    "ConcurrencyConflict",
    "ConsentBackend",
    "ConsentError",
    "ConsentNotActive",
    "ConsentStatus",
    "Decision",
    "DisclosureView",
    "ExportPayload",
    "FIELD_TO_PURPOSE",
    "InMemoryStore",
    "InvalidTransition",
    "NotFound",
    "PermissionDenied",
    "PURPOSE_LABELS",
    "PURPOSE_POLICIES",
    "Purpose",
    "RETENTION_NOTE",
    "ROLE_LABELS",
    "Role",
]
