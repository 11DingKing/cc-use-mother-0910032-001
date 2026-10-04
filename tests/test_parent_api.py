"""家长端 API：授权现状、授权去向与身份校验。"""
from __future__ import annotations

import unittest

from support import (
    GUARDIAN,
    GUARDIAN2,
    MANAGER,
    MINOR,
    OP,
    VOLUNTEER,
    make_api,
)

from minor_consent import GuardianshipError, policies


class ParentApiTest(unittest.TestCase):
    def test_parent_view_consents_shows_versions_validity_roles(self) -> None:
        api, _ = make_api()
        overview = api.parent_view_consents(GUARDIAN, MINOR)
        emergency = overview["purposes"][policies.PURPOSE_EMERGENCY]
        self.assertEqual(emergency["status"], "执行中")
        self.assertEqual(emergency["version"], 4)
        self.assertIn("valid_until", emergency)
        self.assertEqual(
            emergency["visible_roles"]["contact_phone"],
            ["场馆负责人", "文博中心运营员"],
        )
        health = overview["purposes"][policies.PURPOSE_HEALTH]
        self.assertEqual(health["visible_roles"]["medications"], ["场馆负责人"])

    def test_parent_view_disclosure_tracks_where_authorization_went(self) -> None:
        api, _ = make_api()
        api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                  {"contact_phone"}, "incident_response")
        api.read_sensitive_fields(MANAGER, MINOR, policies.PURPOSE_HEALTH,
                                  {"medications"}, "medical_support")
        disclosure = api.parent_view_disclosure(GUARDIAN, MINOR)
        reads = [e for e in disclosure["events"]
                 if e["action"] == "field_read" and e["decision"] == "allow"]
        self.assertEqual(len(reads), 2)
        by_actor = {e["actor_id"]: e for e in reads}
        self.assertEqual(by_actor[OP.actor_id]["fields"], ["contact_phone"])
        self.assertEqual(by_actor[MANAGER.actor_id]["purpose"], policies.PURPOSE_HEALTH)
        # 授权去向只含字段名与决定，不含原值。
        for event in disclosure["events"]:
            self.assertNotIn("value", event)

    def test_disclosure_includes_denied_attempts(self) -> None:
        api, _ = make_api()
        api.start_service(OP, "assign-vol", MINOR, VOLUNTEER.actor_id, "svc-2026-10")
        from minor_consent import AccessDeniedError
        with self.assertRaises(AccessDeniedError):
            api.read_sensitive_fields(VOLUNTEER, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_name"}, "incident_response")
        disclosure = api.parent_view_disclosure(GUARDIAN, MINOR)
        denies = [e for e in disclosure["events"] if e["decision"] == "deny"]
        self.assertTrue(any(e["actor_id"] == VOLUNTEER.actor_id for e in denies))

    def test_other_guardian_and_staff_cannot_use_parent_endpoints(self) -> None:
        api, _ = make_api()
        for endpoint in (
            lambda: api.parent_view_consents(GUARDIAN2, MINOR),
            lambda: api.parent_view_disclosure(GUARDIAN2, MINOR),
            lambda: api.parent_export(GUARDIAN2, MINOR),
            lambda: api.parent_view_consents(OP, MINOR),
            lambda: api.parent_view_disclosure(MANAGER, MINOR),
        ):
            with self.assertRaises(GuardianshipError):
                endpoint()

    def test_parent_withdraw_visible_in_disclosure(self) -> None:
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, policies.PURPOSE_PHOTOS)
        api.parent_withdraw(GUARDIAN, MINOR, policies.PURPOSE_PHOTOS,
                            {"photo_media_release"},
                            expected_version=consent.version, reason="不再授权媒体使用")
        disclosure = api.parent_view_disclosure(GUARDIAN, MINOR)
        withdrawals = [e for e in disclosure["events"] if e["action"] == "consent_withdraw"]
        self.assertEqual(len(withdrawals), 1)
        self.assertEqual(withdrawals[0]["fields"], ["photo_media_release"])
        self.assertEqual(withdrawals[0]["detail"], "不再授权媒体使用")


if __name__ == "__main__":
    unittest.main()
