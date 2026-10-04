"""并发安全：撤回与续期/确认/读取竞争时，已撤销权限绝不复活。"""
from __future__ import annotations

import threading
import unittest
from datetime import timedelta

from support import BASE, GUARDIAN, MINOR, OP, make_api

from minor_consent import AccessDeniedError, ConcurrencyError, policies

PURPOSE = policies.PURPOSE_EMERGENCY
ALL_FIELDS = set(policies.PURPOSE_FIELDS[PURPOSE])


class ConcurrencyTest(unittest.TestCase):
    def test_withdraw_vs_renew_never_resurrects(self) -> None:
        """撤回与续期基于同一版本竞争：恰好一个成功，撤销结果不可被覆盖。"""
        for _ in range(50):
            api, _ = make_api()
            base_version = api.store.get_consent(MINOR, PURPOSE).version
            barrier = threading.Barrier(3)
            outcomes: dict[str, str] = {}

            def do_withdraw() -> None:
                barrier.wait()
                try:
                    api.parent_withdraw(GUARDIAN, MINOR, PURPOSE, None,
                                        expected_version=base_version)
                except ConcurrencyError:
                    outcomes["withdraw"] = "conflict"
                else:
                    outcomes["withdraw"] = "ok"

            def do_renew() -> None:
                barrier.wait()
                try:
                    api.renew_consent(OP, MINOR, PURPOSE, BASE + timedelta(days=90),
                                      expected_version=base_version)
                except ConcurrencyError:
                    outcomes["renew"] = "conflict"
                else:
                    outcomes["renew"] = "ok"

            threads = [threading.Thread(target=do_withdraw),
                       threading.Thread(target=do_renew)]
            for t in threads:
                t.start()
            barrier.wait()
            for t in threads:
                t.join()

            self.assertEqual(sorted(outcomes.values()), ["conflict", "ok"])
            final = api.store.get_consent(MINOR, PURPOSE)
            self.assertEqual(final.version, base_version + 1)
            if outcomes["withdraw"] == "ok":
                # 撤回胜出：授权必须为空，续期不得将其恢复。
                self.assertEqual(final.effective_fields, frozenset())
                self.assertEqual(final.valid_until, BASE + timedelta(days=30))
            else:
                # 续期胜出：撤回未生效，授权保持原样。
                self.assertEqual(final.effective_fields, frozenset(ALL_FIELDS))
                self.assertEqual(final.valid_until, BASE + timedelta(days=90))

    def test_concurrent_reads_never_see_withdrawn_field(self) -> None:
        """撤回完成后，任何并发读取都不得再返回被撤回字段。"""
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, PURPOSE)
        api.parent_withdraw(GUARDIAN, MINOR, PURPOSE, {"contact_phone"},
                            expected_version=consent.version)
        barrier = threading.Barrier(9)
        violations: list[str] = []

        def reader() -> None:
            barrier.wait()
            for _ in range(50):
                try:
                    api.read_sensitive_fields(OP, MINOR, PURPOSE,
                                              {"contact_phone"}, "incident_response")
                except AccessDeniedError as exc:
                    if exc.reason != "field_withdrawn":
                        violations.append(f"unexpected reason: {exc.reason}")
                else:
                    violations.append("read succeeded after withdrawal")

        threads = [threading.Thread(target=reader) for _ in range(8)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        self.assertEqual(violations, [])

    def test_stale_confirm_cannot_resurrect_withdrawn_grant(self) -> None:
        """基于旧版本的确认/续期在撤回之后一律被拒绝。"""
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, PURPOSE)
        stale_version = consent.version
        api.parent_withdraw(GUARDIAN, MINOR, PURPOSE, None,
                            expected_version=stale_version)
        with self.assertRaises(ConcurrencyError):
            api.renew_consent(OP, MINOR, PURPOSE, BASE + timedelta(days=90),
                              expected_version=stale_version)
        # 唯一恢复路径：新草稿 + 监护人显式确认，且只含新草稿列出的字段。
        draft = api.create_draft(OP, MINOR, PURPOSE, {"contact_name"},
                                 BASE, BASE + timedelta(days=30))
        pending = api.submit_consent(OP, MINOR, PURPOSE, draft["version"])
        confirmed = api.confirm_consent(GUARDIAN, MINOR, PURPOSE, pending["version"])
        self.assertEqual(confirmed["granted_fields"], ["contact_name"])

    def test_concurrent_partial_withdraws_merge_without_lost_update(self) -> None:
        """两个并发部分撤回（冲突重试）最终取并集，不丢更新。"""
        api, _ = make_api()

        def withdraw_with_retry(fields: set[str]) -> None:
            while True:
                version = api.store.get_consent(MINOR, PURPOSE).version
                try:
                    api.parent_withdraw(GUARDIAN, MINOR, PURPOSE, fields,
                                        expected_version=version)
                    return
                except ConcurrencyError:
                    continue

        barrier = threading.Barrier(3)
        t1 = threading.Thread(target=lambda: (barrier.wait(),
                                              withdraw_with_retry({"contact_phone"})))
        t2 = threading.Thread(target=lambda: (barrier.wait(),
                                              withdraw_with_retry({"contact_relation"})))
        t1.start()
        t2.start()
        barrier.wait()
        t1.join()
        t2.join()
        final = api.store.get_consent(MINOR, PURPOSE)
        self.assertEqual(final.withdrawn_fields,
                         frozenset({"contact_phone", "contact_relation"}))
        self.assertEqual(final.effective_fields, frozenset({"contact_name"}))

    def test_double_confirm_only_one_wins(self) -> None:
        """同一待核验版本的并发确认只有一个成功。"""
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, PURPOSE)
        api.parent_withdraw(GUARDIAN, MINOR, PURPOSE, None,
                            expected_version=consent.version)
        draft = api.create_draft(OP, MINOR, PURPOSE, {"contact_name"},
                                 BASE, BASE + timedelta(days=30))
        pending = api.submit_consent(OP, MINOR, PURPOSE, draft["version"])
        barrier = threading.Barrier(3)
        results: list[str] = []

        def confirm() -> None:
            barrier.wait()
            try:
                api.confirm_consent(GUARDIAN, MINOR, PURPOSE, pending["version"])
            except Exception as exc:  # noqa: BLE001 - 失败方可能是版本冲突或状态冲突
                results.append(type(exc).__name__)
            else:
                results.append("ok")

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1)
        final = api.store.get_consent(MINOR, PURPOSE)
        self.assertEqual(final.version, pending["version"] + 1)

    def test_audit_chain_valid_after_concurrent_writes(self) -> None:
        api, _ = make_api()
        consent = api.store.get_consent(MINOR, PURPOSE)
        api.parent_withdraw(GUARDIAN, MINOR, PURPOSE, {"contact_phone"},
                            expected_version=consent.version)
        barrier = threading.Barrier(5)

        def hammer() -> None:
            barrier.wait()
            for _ in range(20):
                try:
                    api.read_sensitive_fields(OP, MINOR, PURPOSE,
                                              {"contact_name"}, "incident_response")
                except AccessDeniedError:
                    pass
                try:
                    api.read_sensitive_fields(OP, MINOR, PURPOSE,
                                              {"contact_phone"}, "incident_response")
                except AccessDeniedError:
                    pass

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()
        self.assertTrue(api.store.audit.verify_chain())
        seqs = [e.seq for e in api.store.audit.entries()]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


if __name__ == "__main__":
    unittest.main()
