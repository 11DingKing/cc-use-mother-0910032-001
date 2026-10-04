"""HTTP JSON 接口冒烟测试：签署-读取-撤回-授权去向-导出边界。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from backend_seed import (
    GUARDIAN,
    MINOR_DATA,
    MINOR_ID,
    OPERATOR,
    SERVICE_ID,
    T0,
    make_backend,
)
from consent_backend.server import make_server


def http_json(method: str, url: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ServerSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.backend = make_backend()
        cls.server = make_server(cls.backend, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_consent_read_withdraw_disclosure_flow(self) -> None:
        # 签署
        status, body = http_json("POST", self.url("/v1/consents:grant"), {
            "minor_id": MINOR_ID, "purpose": "health_notes",
            "guardian_id": GUARDIAN, "form_version": "2026-v1",
            "valid_until": "2027-01-01T00:00:00+00:00", "now": T0.isoformat(),
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["status"], "active")

        # 读取：服务关系 + 最小必要字段
        status, body = http_json(
            "GET",
            self.url(f"/v1/minors/{MINOR_ID}/fields?actor_id={OPERATOR}&role=operator&purpose=health_notes"),
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["allowed"])
        self.assertEqual(set(body["fields"]), {"health_allergies"})
        self.assertEqual(body["fields"]["health_allergies"], MINOR_DATA["health_allergies"])

        # 撤回
        status, body = http_json("POST", self.url("/v1/consents:withdraw"), {
            "minor_id": MINOR_ID, "purpose": "health_notes", "guardian_id": GUARDIAN,
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "withdrawn")

        # 撤回后读取被拒
        status, body = http_json(
            "GET",
            self.url(f"/v1/minors/{MINOR_ID}/fields?actor_id={OPERATOR}&role=operator&purpose=health_notes"),
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["allowed"])
        self.assertEqual(body["reason"], "consent_withdrawn")
        self.assertEqual(body["fields"], {})

        # 家长查看授权去向
        status, body = http_json(
            "GET", self.url(f"/v1/minors/{MINOR_ID}/disclosure?guardian_id={GUARDIAN}")
        )
        self.assertEqual(status, 200)
        actions = [t["action"] for t in body["access_trace"]]
        self.assertIn("read_field", actions)
        health = next(c for c in body["consents"] if c["purpose"] == "health_notes")
        self.assertEqual(health["status"], "withdrawn")

        # 员工导出被拒（403）
        status, body = http_json("POST", self.url("/v1/exports"), {
            "requester_id": OPERATOR, "role": "operator", "minor_id": MINOR_ID,
        })
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "permission_denied")

        # 监护人导出成功，且留档元数据可见
        status, body = http_json("POST", self.url("/v1/exports"), {
            "requester_id": GUARDIAN, "role": "guardian", "minor_id": MINOR_ID,
        })
        self.assertEqual(status, 200)
        sections = {s["purpose"]: s for s in body["sections"]}
        self.assertEqual(sections["health_notes"]["consent_status"], "withdrawn")


if __name__ == "__main__":
    unittest.main()
