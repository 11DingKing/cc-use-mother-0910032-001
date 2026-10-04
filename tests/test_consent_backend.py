"""同意生命周期、字段级访问、监护变更、留档导出、审计与家长视图的回归测试。"""
from __future__ import annotations

import unittest

from backend_seed import (
    FAR_FUTURE,
    GUARDIAN,
    MANAGER,
    MINOR_DATA,
    MINOR_ID,
    NEW_GUARDIAN,
    OPERATOR,
    SENSITIVE_VALUES,
    SERVICE_ID,
    T0,
    T1,
    T2,
    VOLUNTEER,
    make_backend,
)
from consent_backend import (
    AuditAction,
    ConsentError,
    ConsentNotActive,
    ConsentStatus,
    Decision,
    PermissionDenied,
    Purpose,
    Role,
)


class ConsentLifecycleTest(unittest.TestCase):
    def test_grant_creates_version_one(self) -> None:
        backend = make_backend()
        record = backend.consents.grant(
            MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN,
            form_version="2026-v1", valid_until=FAR_FUTURE, now=T0,
        )
        self.assertEqual(record.version, 1)
        self.assertEqual(record.status, ConsentStatus.ACTIVE)
        self.assertEqual(record.valid_until, FAR_FUTURE)

    def test_regrant_supersedes_and_bumps_version(self) -> None:
        backend = make_backend(grant_purposes=[Purpose.HEALTH_NOTES])
        record = backend.consents.grant(
            MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN,
            form_version="2026-v2", valid_until=FAR_FUTURE, now=T1,
        )
        self.assertEqual(record.version, 2)
        chain = backend.store.consent_chain(MINOR_ID, Purpose.HEALTH_NOTES)
        self.assertEqual(
            [(r.version, r.status) for r in chain],
            [
                (1, ConsentStatus.ACTIVE),
                (1, ConsentStatus.SUPERSEDED),
                (2, ConsentStatus.ACTIVE),
            ],
        )

    def test_partial_withdrawal_only_affects_one_purpose(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        backend.consents.withdraw(MINOR_ID, Purpose.SERVICE_PHOTOS, GUARDIAN, now=T1)
        photos = backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.SERVICE_PHOTOS, now=T1)
        self.assertFalse(photos.allowed)
        self.assertEqual(photos.reason, "consent_withdrawn")
        # 其他用途不受影响
        health = backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.HEALTH_NOTES, now=T1)
        self.assertTrue(health.allowed)
        emergency = backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.EMERGENCY_CONTACT, now=T1)
        self.assertTrue(emergency.allowed)

    def test_expiry_blocks_reads(self) -> None:
        backend = make_backend()
        backend.consents.grant(
            MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN,
            form_version="2026-v1", valid_until=T1, now=T0,
        )
        result = backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.HEALTH_NOTES, now=T2)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "consent_expired")
        head = backend.consents.current_status(MINOR_ID, Purpose.HEALTH_NOTES, now=T2)
        self.assertEqual(head.status, ConsentStatus.EXPIRED)

    def test_withdraw_requires_active_consent(self) -> None:
        backend = make_backend(grant_purposes=[Purpose.HEALTH_NOTES])
        backend.consents.withdraw(MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN, now=T1)
        with self.assertRaises(ConsentNotActive):
            backend.consents.withdraw(MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN, now=T2)

    def test_grant_requires_current_guardian(self) -> None:
        backend = make_backend()
        with self.assertRaises(PermissionDenied):
            backend.consents.grant(
                MINOR_ID, Purpose.HEALTH_NOTES, "stranger",
                form_version="2026-v1", now=T0,
            )

    def test_valid_until_must_follow_valid_from(self) -> None:
        backend = make_backend()
        with self.assertRaises(ConsentError):
            backend.consents.grant(
                MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN,
                form_version="2026-v1", valid_from=T1, valid_until=T0, now=T0,
            )


class AccessControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = make_backend(grant_purposes=list(Purpose))

    def test_operator_gets_minimal_fields_not_all(self) -> None:
        result = self.backend.access.read_fields(
            OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.EMERGENCY_CONTACT, now=T0
        )
        self.assertTrue(result.allowed)
        self.assertEqual(
            set(result.fields),
            {"emergency_contact_name", "emergency_contact_phone"},
        )
        self.assertNotIn("emergency_contact_relation", result.fields)

    def test_manager_gets_full_policy_fields(self) -> None:
        result = self.backend.access.read_fields(
            MANAGER, Role.VENUE_MANAGER, MINOR_ID, Purpose.EMERGENCY_CONTACT, now=T0
        )
        self.assertTrue(result.allowed)
        self.assertEqual(len(result.fields), 3)

    def test_volunteer_denied_role_not_permitted(self) -> None:
        self.backend.add_assignment(
            assignment_id="asg-vol", staff_id=VOLUNTEER, role=Role.VOLUNTEER,
            minor_id=MINOR_ID, service_id=SERVICE_ID,
            purposes=set(Purpose), start=T0,
        )
        result = self.backend.access.read_fields(
            VOLUNTEER, Role.VOLUNTEER, MINOR_ID, Purpose.HEALTH_NOTES, now=T0
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "role_not_permitted")
        self.assertEqual(result.fields, {})

    def test_staff_without_assignment_denied(self) -> None:
        result = self.backend.access.read_fields(
            "op-2", Role.OPERATOR, MINOR_ID, Purpose.HEALTH_NOTES, now=T0
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "no_active_assignment")

    def test_assignment_scope_limits_purpose(self) -> None:
        self.backend.add_assignment(
            assignment_id="asg-op2", staff_id="op-2", role=Role.OPERATOR,
            minor_id=MINOR_ID, service_id=SERVICE_ID,
            purposes={Purpose.EMERGENCY_CONTACT}, start=T0,
        )
        result = self.backend.access.read_fields(
            "op-2", Role.OPERATOR, MINOR_ID, Purpose.HEALTH_NOTES, now=T0
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "purpose_not_in_assignment_scope")

    def test_ended_assignment_denied(self) -> None:
        self.backend.end_assignment("asg-op", now=T1)
        result = self.backend.access.read_fields(
            OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.HEALTH_NOTES, now=T1
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "no_active_assignment")

    def test_withdrawn_consent_denied_for_all_staff(self) -> None:
        # 家长投诉场景：撤回后任何岗位都读不到。
        self.backend.consents.withdraw(MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN, now=T1)
        for actor, role in ((OPERATOR, Role.OPERATOR), (MANAGER, Role.VENUE_MANAGER)):
            result = self.backend.access.read_fields(actor, role, MINOR_ID, Purpose.HEALTH_NOTES, now=T1)
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason, "consent_withdrawn")
            self.assertEqual(result.fields, {})

    def test_guardian_self_access_full_fields_even_after_withdraw(self) -> None:
        self.backend.consents.withdraw(MINOR_ID, Purpose.EMERGENCY_CONTACT, GUARDIAN, now=T1)
        result = self.backend.access.read_fields(
            GUARDIAN, Role.GUARDIAN, MINOR_ID, Purpose.EMERGENCY_CONTACT, now=T1
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "guardian_self_access")
        self.assertEqual(len(result.fields), 3)

    def test_requested_fields_narrowing_and_unknown_names_ignored(self) -> None:
        result = self.backend.access.read_fields(
            OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.EMERGENCY_CONTACT,
            requested_fields=["emergency_contact_phone", "password"], now=T0,
        )
        self.assertEqual(set(result.fields), {"emergency_contact_phone"})
        entry = self.backend.audit_trail(MINOR_ID)[-1]
        self.assertNotIn("password", entry.field_names)

    def test_data_write_requires_consent(self) -> None:
        backend = make_backend()  # 未签署任何同意
        with self.assertRaises(PermissionDenied):
            backend.data.update_fields(
                OPERATOR, Role.OPERATOR, MINOR_ID,
                {"health_allergies": "青霉素过敏"}, now=T0,
            )
        # 监护人本人可随时更新
        record = backend.data.update_fields(
            GUARDIAN, Role.GUARDIAN, MINOR_ID,
            {"health_allergies": "青霉素过敏"}, now=T0,
        )
        self.assertEqual(record.data["health_allergies"], "青霉素过敏")

    def test_data_write_blocked_after_withdrawal(self) -> None:
        self.backend.consents.withdraw(MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN, now=T1)
        with self.assertRaises(PermissionDenied):
            self.backend.data.update_fields(
                OPERATOR, Role.OPERATOR, MINOR_ID,
                {"health_allergies": "青霉素过敏"}, now=T1,
            )


class GuardianshipChangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = make_backend(grant_purposes=list(Purpose))
        self.backend.guardianships.change(
            MINOR_ID, NEW_GUARDIAN, actor_id="admin", now=T1
        )

    def test_change_invalidates_active_consents(self) -> None:
        result = self.backend.access.read_fields(
            OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.HEALTH_NOTES, now=T1
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "consent_invalidated_guardianship")

    def test_old_guardian_loses_access_and_portal(self) -> None:
        result = self.backend.access.read_fields(
            GUARDIAN, Role.GUARDIAN, MINOR_ID, Purpose.HEALTH_NOTES, now=T1
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "not_current_guardian")
        with self.assertRaises(PermissionDenied):
            self.backend.portal.disclosure_view(GUARDIAN, MINOR_ID, now=T1)

    def test_new_guardian_regrants_with_bumped_version(self) -> None:
        record = self.backend.consents.grant(
            MINOR_ID, Purpose.HEALTH_NOTES, NEW_GUARDIAN,
            form_version="2026-v1", valid_until=FAR_FUTURE, now=T2,
        )
        self.assertEqual(record.version, 2)
        result = self.backend.access.read_fields(
            OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.HEALTH_NOTES, now=T2
        )
        self.assertTrue(result.allowed)

    def test_history_preserved_for_disclosure(self) -> None:
        view = self.backend.portal.disclosure_view(NEW_GUARDIAN, MINOR_ID, now=T1)
        self.assertEqual(len(view.guardianship_history), 2)
        self.assertEqual(view.guardianship_history[0].guardian_id, GUARDIAN)
        self.assertIsNotNone(view.guardianship_history[0].effective_to)
        health = next(c for c in view.consents if c.purpose == Purpose.HEALTH_NOTES)
        self.assertEqual(health.status, ConsentStatus.GUARDIANSHIP_INVALIDATED.value)


class ArchiveExportTest(unittest.TestCase):
    def test_archive_freezes_only_consented_purposes(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        backend.consents.withdraw(MINOR_ID, Purpose.SERVICE_PHOTOS, GUARDIAN, now=T1)
        archive = backend.archives.create_archive(MANAGER, Role.VENUE_MANAGER, MINOR_ID, SERVICE_ID, now=T2)
        self.assertEqual(
            set(archive.snapshots),
            {Purpose.EMERGENCY_CONTACT, Purpose.HEALTH_NOTES},
        )

    def test_archive_survives_withdrawal_and_reads_are_audited(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        archive = backend.archives.create_archive(MANAGER, Role.VENUE_MANAGER, MINOR_ID, SERVICE_ID, now=T1)
        backend.consents.withdraw(MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN, now=T2)
        # 留档边界：撤回不删除留档，但读取需理由且全程审计。
        snapshot = backend.archives.read_archive(
            MANAGER, Role.VENUE_MANAGER, archive.archive_id, Purpose.HEALTH_NOTES,
            justification="家长投诉核查需要调阅留档", now=T2,
        )
        self.assertEqual(snapshot.values["health_allergies"], MINOR_DATA["health_allergies"])
        reads = [
            e for e in backend.audit_trail(MINOR_ID)
            if e.action == AuditAction.ARCHIVE_READ and e.decision == Decision.ALLOW
        ]
        self.assertEqual(len(reads), 1)
        view = backend.portal.disclosure_view(GUARDIAN, MINOR_ID, now=T2)
        self.assertTrue(any(t.action == AuditAction.ARCHIVE_READ.value for t in view.access_trace))
        self.assertEqual(len(view.archives), 1)

    def test_archive_read_requires_role_and_justification(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        archive = backend.archives.create_archive(MANAGER, Role.VENUE_MANAGER, MINOR_ID, SERVICE_ID, now=T1)
        with self.assertRaises(PermissionDenied):
            backend.archives.read_archive(
                VOLUNTEER, Role.VOLUNTEER, archive.archive_id, Purpose.HEALTH_NOTES,
                justification="家长投诉核查需要调阅留档", now=T1,
            )
        with self.assertRaises(PermissionDenied):
            backend.archives.read_archive(
                MANAGER, Role.VENUE_MANAGER, archive.archive_id, Purpose.HEALTH_NOTES,
                justification="看看", now=T1,
            )
        denies = [
            e for e in backend.audit_trail(MINOR_ID)
            if e.action == AuditAction.ARCHIVE_READ and e.decision == Decision.DENY
        ]
        self.assertEqual(len(denies), 2)

    def test_operator_archive_requires_service_relationship(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        with self.assertRaises(PermissionDenied):
            backend.archives.create_archive(OPERATOR, Role.OPERATOR, MINOR_ID, "svc-x", now=T1)

    def test_export_guardian_only(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        with self.assertRaises(PermissionDenied):
            backend.exports.request_export(OPERATOR, Role.OPERATOR, MINOR_ID, now=T0)
        deny = backend.audit_trail(MINOR_ID)[-1]
        self.assertEqual(deny.action, AuditAction.EXPORT)
        self.assertEqual(deny.decision, Decision.DENY)

    def test_export_annotates_consent_status_and_keeps_metadata_only(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        backend.consents.withdraw(MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN, now=T1)
        payload = backend.exports.request_export(GUARDIAN, Role.GUARDIAN, MINOR_ID, now=T2)
        sections = {s.purpose: s for s in payload.sections}
        self.assertEqual(sections[Purpose.HEALTH_NOTES].consent_status, "withdrawn")
        # 监护人本人的导出副本仍包含数据，并标注用途状态。
        self.assertEqual(
            sections[Purpose.EMERGENCY_CONTACT].fields["emergency_contact_phone"],
            MINOR_DATA["emergency_contact_phone"],
        )
        # 系统侧导出记录只留元数据，不复制原值。
        records = backend.store.exports_for_minor(MINOR_ID)
        self.assertEqual(len(records), 1)
        for value in SENSITIVE_VALUES:
            self.assertNotIn(value, repr(records[0]))


class AuditTest(unittest.TestCase):
    def test_every_decision_logged_with_reason_and_version(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.EMERGENCY_CONTACT, now=T0)
        backend.access.read_fields("op-2", Role.OPERATOR, MINOR_ID, Purpose.EMERGENCY_CONTACT, now=T0)
        entries = [
            e for e in backend.audit_trail(MINOR_ID) if e.action == AuditAction.READ_FIELD
        ]
        self.assertEqual(len(entries), 2)
        allow, deny = entries
        self.assertEqual(allow.decision, Decision.ALLOW)
        self.assertEqual(allow.consent_version, 1)
        self.assertEqual(
            set(allow.field_names),
            {"emergency_contact_name", "emergency_contact_phone"},
        )
        self.assertEqual(deny.decision, Decision.DENY)
        self.assertEqual(deny.reason, "no_active_assignment")

    def test_audit_never_contains_raw_values(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.EMERGENCY_CONTACT, now=T0)
        backend.access.read_fields(MANAGER, Role.VENUE_MANAGER, MINOR_ID, Purpose.HEALTH_NOTES, now=T0)
        backend.exports.request_export(GUARDIAN, Role.GUARDIAN, MINOR_ID, now=T0)
        archive = backend.archives.create_archive(MANAGER, Role.VENUE_MANAGER, MINOR_ID, SERVICE_ID, now=T0)
        backend.archives.read_archive(
            MANAGER, Role.VENUE_MANAGER, archive.archive_id, Purpose.HEALTH_NOTES,
            justification="家长投诉核查需要调阅留档", now=T0,
        )
        backend.portal.disclosure_view(GUARDIAN, MINOR_ID, now=T0)
        trail = backend.audit_trail()
        self.assertGreater(len(trail), 0)
        for entry in trail:
            for value in SENSITIVE_VALUES:
                self.assertNotIn(value, repr(entry))


class ParentPortalTest(unittest.TestCase):
    def test_disclosure_view_shows_where_authorization_went(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.EMERGENCY_CONTACT, now=T0)
        backend.exports.request_export(GUARDIAN, Role.GUARDIAN, MINOR_ID, now=T1)
        view = backend.portal.disclosure_view(GUARDIAN, MINOR_ID, now=T2)
        self.assertEqual(len(view.consents), 3)
        health = next(c for c in view.consents if c.purpose == Purpose.HEALTH_NOTES)
        self.assertEqual(health.status, "active")
        self.assertEqual(health.version, 1)
        self.assertIn("文博中心运营员", health.visible_roles)
        # 授权去向：谁在何时读了哪些字段、导出了什么。
        reads = [t for t in view.access_trace if t.action == AuditAction.READ_FIELD.value]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].actor_role, "文博中心运营员")
        self.assertEqual(
            set(reads[0].field_names),
            {"emergency_contact_name", "emergency_contact_phone"},
        )
        self.assertEqual(len(view.exports), 1)

    def test_non_guardian_denied_and_audited(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        with self.assertRaises(PermissionDenied):
            backend.portal.disclosure_view(OPERATOR, MINOR_ID, now=T0)
        entry = backend.audit_trail(MINOR_ID)[-1]
        self.assertEqual(entry.action, AuditAction.DISCLOSURE_VIEW)
        self.assertEqual(entry.decision, Decision.DENY)


if __name__ == "__main__":
    unittest.main()
