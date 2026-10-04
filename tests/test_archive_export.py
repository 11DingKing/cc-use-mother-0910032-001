"""历史留档边界与导出：归档、合规读取、数据主体导出与留档销毁。"""
from __future__ import annotations

import unittest
from datetime import timedelta

from support import (
    BASE,
    GUARDIAN,
    MANAGER,
    MINOR,
    OP,
    SECRET_PHONE,
    make_api,
)

from minor_consent import AccessDeniedError, ConsentStateError, GuardianshipError, policies


def _archive_all(api) -> None:
    api.end_service(OP, "assign-op")
    api.end_service(OP, "assign-mgr")
    for purpose in (policies.PURPOSE_EMERGENCY, policies.PURPOSE_HEALTH,
                    policies.PURPOSE_PHOTOS):
        consent = api.store.get_consent(MINOR, purpose)
        api.archive_consent(MANAGER, MINOR, purpose, consent.version)


class ArchiveExportTest(unittest.TestCase):
    def test_archive_requires_no_active_service(self) -> None:
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
        with self.assertRaises(ConsentStateError):
            api.archive_consent(MANAGER, MINOR, policies.PURPOSE_EMERGENCY,
                                consent.version)

    def test_archived_records_deny_staff_reads(self) -> None:
        api, _ = make_api()
        _archive_all(api)
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_name"}, "incident_response")
        self.assertEqual(ctx.exception.reason, "archived")
        # 归档记录不可再变更。
        consent = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
        with self.assertRaises(ConsentStateError):
            api.archive_consent(MANAGER, MINOR, policies.PURPOSE_EMERGENCY,
                                consent.version)

    def test_compliance_read_within_retention(self) -> None:
        api, _ = make_api()
        _archive_all(api)
        values = api.compliance_read(MANAGER, MINOR, policies.PURPOSE_EMERGENCY,
                                     {"contact_phone"})
        self.assertEqual(values["contact_phone"], SECRET_PHONE)
        last = api.store.audit.entries()[-1]
        self.assertEqual(last.action, "compliance_read")
        self.assertEqual(last.decision, "allow")
        # 运营员没有合规通道。
        with self.assertRaises(AccessDeniedError):
            api.compliance_read(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                {"contact_phone"})

    def test_guardian_export_marks_sealed_and_keeps_values(self) -> None:
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, policies.PURPOSE_PHOTOS)
        api.parent_withdraw(GUARDIAN, MINOR, policies.PURPOSE_PHOTOS,
                            {"photo_media_release"}, expected_version=consent.version)
        bundle = api.parent_export(GUARDIAN, MINOR)
        photos = bundle["purposes"][policies.PURPOSE_PHOTOS]
        self.assertTrue(photos["values"]["photo_media_release"]["sealed"])
        self.assertIsNotNone(photos["values"]["photo_media_release"]["value"])
        self.assertFalse(photos["values"]["photo_public_display"]["sealed"])
        # 导出本身留痕。
        exports = api.store.exports_of_minor(MINOR)
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0].requester_id, GUARDIAN.actor_id)

    def test_export_contains_disclosure_and_history(self) -> None:
        api, _ = make_api()
        api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                  {"contact_phone"}, "incident_response")
        bundle = api.parent_export(GUARDIAN, MINOR)
        disclosures = bundle["access_disclosure"]
        reads = [d for d in disclosures
                 if d["action"] == "field_read" and d["decision"] == "allow"]
        self.assertTrue(any(d["actor_id"] == OP.actor_id for d in reads))
        versions = [h["version"] for h in bundle["consent_history"]
                    if h["purpose"] == policies.PURPOSE_EMERGENCY]
        self.assertEqual(versions, [1, 2, 3, 4])

    def test_purge_after_retention_destroys_values(self) -> None:
        api, clock = make_api()
        _archive_all(api)
        clock.set(BASE + timedelta(days=31 + policies.RETENTION_DAYS_AFTER_ARCHIVE + 1))
        result = api.purge_expired_archives(MANAGER)
        self.assertEqual(len(result["purged"]), 3)
        # 合规读取只能看到“已销毁”。
        with self.assertRaises(AccessDeniedError) as ctx:
            api.compliance_read(MANAGER, MINOR, policies.PURPOSE_EMERGENCY,
                                {"contact_phone"})
        self.assertEqual(ctx.exception.reason, "purged")
        # 监护人导出显示墓碑而非原值。
        bundle = api.parent_export(GUARDIAN, MINOR)
        values = bundle["purposes"][policies.PURPOSE_EMERGENCY]["values"]
        self.assertTrue(values["contact_phone"]["destroyed"])
        self.assertIsNone(values["contact_phone"]["value"])

    def test_purge_before_retention_is_noop(self) -> None:
        api, _ = make_api()
        _archive_all(api)
        result = api.purge_expired_archives(MANAGER)
        self.assertEqual(result["purged"], [])
        values = api.compliance_read(MANAGER, MINOR, policies.PURPOSE_EMERGENCY,
                                     {"contact_phone"})
        self.assertEqual(values["contact_phone"], SECRET_PHONE)

    def test_operator_cannot_purge(self) -> None:
        api, _ = make_api()
        with self.assertRaises(GuardianshipError):
            api.purge_expired_archives(OP)


if __name__ == "__main__":
    unittest.main()
