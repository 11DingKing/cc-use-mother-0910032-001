"""字段级访问控制：服务关系、可见角色、最小必要范围与审计不泄露原值。"""
from __future__ import annotations

import json
import unittest

from support import (
    GUARDIAN,
    MANAGER,
    MINOR,
    OP,
    OP2,
    SECRET_ALLERGY,
    SECRET_PHONE,
    SECRET_PHOTO,
    VOLUNTEER,
    make_api,
)

from minor_consent import AccessDeniedError, policies


class AccessControlTest(unittest.TestCase):
    def test_allowed_read_writes_allow_audit(self) -> None:
        api, _ = make_api()
        values = api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                           {"contact_phone"}, "incident_response")
        self.assertEqual(values, {"contact_phone": SECRET_PHONE})
        last = api.store.audit.entries()[-1]
        self.assertEqual(last.action, "field_read")
        self.assertEqual(last.decision, "allow")
        self.assertEqual(last.fields, ("contact_phone",))

    def test_volunteer_has_no_sensitive_access(self) -> None:
        api, _ = make_api()
        api.start_service(OP, "assign-vol", MINOR, VOLUNTEER.actor_id, "svc-2026-10")
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(VOLUNTEER, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_name"}, "incident_response")
        self.assertEqual(ctx.exception.reason, "role_not_visible")

    def test_no_service_relation_denies_even_for_visible_role(self) -> None:
        api, _ = make_api()
        # OP2 角色可见但未在该未成年人的服务中在岗。
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(OP2, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_name"}, "incident_response")
        self.assertEqual(ctx.exception.reason, "no_service_relation")

    def test_ended_service_relation_revokes_access(self) -> None:
        api, _ = make_api()
        api.end_service(OP, "assign-op")
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_name"}, "incident_response")
        self.assertEqual(ctx.exception.reason, "no_service_relation")

    def test_field_level_role_matrix(self) -> None:
        api, _ = make_api()
        # medications 仅场馆负责人可见，运营员被拒。
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_HEALTH,
                                      {"medications"}, "medical_support")
        self.assertEqual(ctx.exception.reason, "role_not_visible")
        values = api.read_sensitive_fields(MANAGER, MINOR, policies.PURPOSE_HEALTH,
                                           {"medications"}, "medical_support")
        self.assertEqual(values["medications"], "无")

    def test_minimum_necessary_scope_enforced(self) -> None:
        api, _ = make_api()
        # 餐饮安排只需要过敏信息，请求用药史超出最小必要范围。
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(MANAGER, MINOR, policies.PURPOSE_HEALTH,
                                      {"allergies", "medications"}, "meal_planning")
        self.assertEqual(ctx.exception.reason, "exceeds_minimum_necessary")
        values = api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_HEALTH,
                                           {"allergies"}, "meal_planning")
        self.assertEqual(values, {"allergies": SECRET_ALLERGY})

    def test_unknown_task_denies_everything(self) -> None:
        api, _ = make_api()
        with self.assertRaises(AccessDeniedError) as ctx:
            api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_PHOTOS,
                                      {"photo_public_display"}, "marketing_push")
        self.assertEqual(ctx.exception.reason, "exceeds_minimum_necessary")

    def test_guardian_cannot_use_staff_channel(self) -> None:
        api, _ = make_api()
        with self.assertRaises(AccessDeniedError):
            api.read_sensitive_fields(GUARDIAN, MINOR, policies.PURPOSE_EMERGENCY,
                                      {"contact_name"}, "incident_response")

    def test_denials_are_audited_with_reason(self) -> None:
        api, _ = make_api()
        for task in ("incident_response",):
            with self.assertRaises(AccessDeniedError):
                api.read_sensitive_fields(OP2, MINOR, policies.PURPOSE_EMERGENCY,
                                          {"contact_name"}, task)
        denies = [e for e in api.store.audit.entries() if e.decision == "deny"]
        self.assertTrue(denies)
        self.assertEqual(denies[-1].reason, "no_service_relation")
        self.assertEqual(denies[-1].actor_id, OP2.actor_id)

    def test_audit_never_contains_raw_values(self) -> None:
        api, _ = make_api()
        api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_EMERGENCY,
                                  {"contact_phone"}, "incident_response")
        api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_HEALTH,
                                  {"allergies"}, "meal_planning")
        api.read_sensitive_fields(OP, MINOR, policies.PURPOSE_PHOTOS,
                                  {"photo_public_display"}, "newsletter")
        api.parent_export(GUARDIAN, MINOR)
        blob = json.dumps([e.payload() for e in api.store.audit.entries()],
                          ensure_ascii=False)
        for secret in (SECRET_PHONE, SECRET_ALLERGY, SECRET_PHOTO, "张三", "哮喘"):
            self.assertNotIn(secret, blob)
        self.assertTrue(api.store.audit.verify_chain())


if __name__ == "__main__":
    unittest.main()
