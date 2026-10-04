"""监护关系变更：挂起、重新核验与旧监护人权限失效。"""
from __future__ import annotations

import unittest

from support import GUARDIAN, GUARDIAN2, MANAGER, MINOR, OP, make_api

from minor_consent import AccessDeniedError, GuardianshipError, policies


class GuardianshipTest(unittest.TestCase):
    def test_transfer_suspends_all_active_consents(self) -> None:
        api, _ = make_api()
        api.transfer_guardianship(MANAGER, MINOR, GUARDIAN2.actor_id, "父亲",
                                  evidence="法院调解书-2026-001")
        for purpose in (policies.PURPOSE_EMERGENCY, policies.PURPOSE_HEALTH,
                        policies.PURPOSE_PHOTOS):
            consent = api.store.get_consent(MINOR, purpose)
            self.assertEqual(consent.status.value, "待核验")
            self.assertTrue(consent.suspended)
            with self.assertRaises(AccessDeniedError) as ctx:
                api.read_sensitive_fields(OP, MINOR, purpose,
                                          set(consent.effective_fields) or {"contact_name"},
                                          "incident_response")
            self.assertIn(ctx.exception.reason, {"consent_not_active", "consent_suspended"})

    def test_old_guardian_loses_access_immediately(self) -> None:
        api, _ = make_api()
        api.transfer_guardianship(MANAGER, MINOR, GUARDIAN2.actor_id, "父亲",
                                  evidence="法院调解书-2026-001")
        with self.assertRaises(GuardianshipError):
            api.parent_view_consents(GUARDIAN, MINOR)
        with self.assertRaises(GuardianshipError):
            api.parent_export(GUARDIAN, MINOR)
        # 新监护人可以查看。
        overview = api.parent_view_consents(GUARDIAN2, MINOR)
        self.assertIn(policies.PURPOSE_EMERGENCY, overview["purposes"])

    def test_new_guardian_reconfirm_restores_access(self) -> None:
        api, _ = make_api()
        api.transfer_guardianship(MANAGER, MINOR, GUARDIAN2.actor_id, "父亲",
                                  evidence="法院调解书-2026-001")
        suspended = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
        confirmed = api.confirm_consent(GUARDIAN2, MINOR, policies.PURPOSE_EMERGENCY,
                                        suspended.version)
        self.assertEqual(confirmed["status"], "执行中")
        values = api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                           {"contact_phone"}, "incident_response")
        self.assertIn("contact_phone", values)

    def test_old_guardian_cannot_confirm_after_transfer(self) -> None:
        api, _ = make_api()
        api.transfer_guardianship(MANAGER, MINOR, GUARDIAN2.actor_id, "父亲",
                                  evidence="法院调解书-2026-001")
        suspended = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
        with self.assertRaises(GuardianshipError):
            api.confirm_consent(GUARDIAN, MINOR, policies.PURPOSE_EMERGENCY,
                                suspended.version)

    def test_transfer_keeps_history_for_archive(self) -> None:
        api, _ = make_api()
        api.transfer_guardianship(MANAGER, MINOR, GUARDIAN2.actor_id, "父亲",
                                  evidence="法院调解书-2026-001")
        history = api.store.guardian_history(MINOR)
        self.assertEqual([g.guardian_id for g in history],
                         [GUARDIAN.actor_id, GUARDIAN2.actor_id])
        # 历史同意版本完整留档。
        versions = api.store.consent_history(MINOR, policies.PURPOSE_HEALTH)
        self.assertGreaterEqual(len(versions), 4)  # 草拟/待核验/执行中/挂起

    def test_operator_cannot_transfer_guardianship(self) -> None:
        api, _ = make_api()
        with self.assertRaises(GuardianshipError):
            api.transfer_guardianship(OP, MINOR, GUARDIAN2.actor_id, "父亲",
                                      evidence="x")


if __name__ == "__main__":
    unittest.main()
