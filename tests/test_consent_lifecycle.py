"""同意生命周期：版本、有效期、部分撤回、续期与重新授权边界。"""
from __future__ import annotations

import unittest
from datetime import timedelta

from support import (
    BASE,
    GUARDIAN,
    MINOR,
    OP,
    SECRET_PHONE,
    Clock,
    grant,
    make_api,
)

from minor_consent import ConsentStateError, NotFoundError, policies
from minor_consent.errors import AccessDeniedError


class ConsentLifecycleTest(unittest.TestCase):
    def test_full_flow_uses_contract_states(self) -> None:
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
        self.assertEqual(consent.status.value, "执行中")  # 已有服务关系，确认后进入执行中
        history = api.store.consent_history(MINOR, policies.PURPOSE_EMERGENCY)
        self.assertEqual([v.status.value for v in history],
                         ["草拟", "待核验", "已确认", "执行中"])
        self.assertEqual([v.version for v in history], [1, 2, 3, 4])

    def test_confirmed_without_service_is_confirmed_status(self) -> None:
        api, _ = make_api(with_service=False)
        consent = api.store.get_consent(MINOR, policies.PURPOSE_HEALTH)
        self.assertEqual(consent.status.value, "已确认")

    def test_partial_withdraw_keeps_other_fields(self) -> None:
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
        result = api.parent_withdraw(
            GUARDIAN, MINOR, policies.PURPOSE_EMERGENCY,
            {"contact_phone"}, expected_version=consent.version, reason="换号",
        )
        self.assertEqual(result["withdrawn_fields"], ["contact_phone"])
        self.assertEqual(sorted(result["effective_fields"]),
                         ["contact_name", "contact_relation"])
        # 被撤回字段立即拒读，其余字段不受影响。
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_phone"}, "incident_response")
        self.assertEqual(ctx.exception.reason, "field_withdrawn")
        values = api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                           {"contact_name"}, "incident_response")
        self.assertEqual(values["contact_name"], "张三")

    def test_full_withdraw_blocks_every_field(self) -> None:
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, policies.PURPOSE_HEALTH)
        api.parent_withdraw(GUARDIAN, MINOR, policies.PURPOSE_HEALTH,
                            None, expected_version=consent.version)
        for field_name in ("allergies", "medications"):
            with self.assertRaises(AccessDeniedError):
                api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_HEALTH,
                                          {field_name}, "medical_support")

    def test_expired_consent_denies_reads(self) -> None:
        api, clock = make_api()
        clock.advance(days=31)  # 超出 30 天有效期
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_name"}, "incident_response")
        self.assertEqual(ctx.exception.reason, "consent_expired")

    def test_renew_extends_validity_but_not_withdrawn_fields(self) -> None:
        api, clock = make_api()
        consent = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
        v = api.parent_withdraw(GUARDIAN, MINOR, policies.PURPOSE_EMERGENCY,
                                {"contact_phone"}, expected_version=consent.version)
        renewed = api.renew_consent(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                    BASE + timedelta(days=90), v["version"])
        self.assertEqual(renewed["withdrawn_fields"], ["contact_phone"])
        clock.advance(days=60)
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_phone"}, "incident_response")
        self.assertEqual(ctx.exception.reason, "field_withdrawn")
        values = api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                           {"contact_name"}, "incident_response")
        self.assertEqual(values["contact_name"], "张三")

    def test_regrant_requires_new_draft_and_confirm(self) -> None:
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
        api.parent_withdraw(GUARDIAN, MINOR, policies.PURPOSE_EMERGENCY,
                            None, expected_version=consent.version)
        # 重新授权只能走完整流程，且只恢复新草稿显式列出的字段。
        draft = api.create_draft(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                 {"contact_name", "contact_relation"},
                                 BASE, BASE + timedelta(days=30))
        pending = api.submit_consent(OP, MINOR, policies.PURPOSE_EMERGENCY, draft["version"])
        confirmed = api.confirm_consent(GUARDIAN, MINOR, policies.PURPOSE_EMERGENCY,
                                        pending["version"])
        self.assertNotIn("contact_phone", confirmed["granted_fields"])
        with self.assertRaises(AccessDeniedError):
            api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_phone"}, "incident_response")

    def test_draft_rejected_while_grant_active(self) -> None:
        api, _ = make_api()
        with self.assertRaises(ConsentStateError):
            api.create_draft(OP, MINOR, policies.PURPOSE_EMERGENCY,
                             {"contact_name"}, BASE, BASE + timedelta(days=60))

    def test_unknown_purpose_and_field_rejected(self) -> None:
        api, _ = make_api()
        with self.assertRaises(NotFoundError):
            api.create_draft(OP, MINOR, "location_tracking",
                             {"gps"}, BASE, BASE + timedelta(days=1))
        with self.assertRaises(ConsentStateError):
            api.create_draft(OP, MINOR, policies.PURPOSE_HEALTH,
                             {"contact_phone"}, BASE, BASE + timedelta(days=1))

    def test_non_guardian_cannot_withdraw(self) -> None:
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, policies.PURPOSE_PHOTOS)
        with self.assertRaises(Exception) as ctx:
            api.parent_withdraw(OP, MINOR, policies.PURPOSE_PHOTOS,
                                {"photo_media_release"}, expected_version=consent.version)
        self.assertEqual(getattr(ctx.exception, "reason", ""), "guardianship_error")


if __name__ == "__main__":
    unittest.main()
