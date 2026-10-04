"""并发回归：撤回不被并发更新恢复、CAS 单胜者、监护变更与签署竞态。"""
from __future__ import annotations

import threading
import unittest
from dataclasses import replace

from backend_seed import (
    FAR_FUTURE,
    GUARDIAN,
    MINOR_ID,
    NEW_GUARDIAN,
    OPERATOR,
    T0,
    make_backend,
)
from consent_backend import (
    ConcurrencyConflict,
    ConsentNotActive,
    ConsentStatus,
    InvalidTransition,
    PermissionDenied,
    Purpose,
    Role,
)
from consent_backend.domain import TERMINAL_STATUSES, ConsentRecord


def run_threads(workers) -> None:
    barrier = threading.Barrier(len(workers))
    threads = []
    for worker in workers:
        def wrapped(fn=worker):
            barrier.wait()
            fn()
        thread = threading.Thread(target=wrapped)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout=30)
    for thread in threads:
        assert not thread.is_alive(), "并发用例超时"


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_grants_single_winner(self) -> None:
        backend = make_backend()
        winners, conflicts = [], []

        def worker(i):
            try:
                backend.consents.grant(
                    MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN,
                    form_version=f"2026-v{i}", valid_until=FAR_FUTURE,
                    expected_head_seq=0, now=T0,
                )
                winners.append(i)
            except ConcurrencyConflict:
                conflicts.append(i)

        run_threads([lambda i=i: worker(i) for i in range(8)])
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 7)
        chain = backend.store.consent_chain(MINOR_ID, Purpose.HEALTH_NOTES)
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].version, 1)

    def test_stale_update_cannot_restore_withdrawn_consent(self) -> None:
        backend = make_backend(grant_purposes=[Purpose.HEALTH_NOTES])
        head_before = backend.store.chain_head(MINOR_ID, Purpose.HEALTH_NOTES)
        backend.consents.withdraw(MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN, now=T0)

        # 基于过期快照的并发写入被 CAS 拒绝
        with self.assertRaises(ConcurrencyConflict):
            backend.consents.grant(
                MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN,
                form_version="2026-v2", valid_until=FAR_FUTURE,
                expected_head_seq=head_before.seq, now=T0,
            )
        # 状态机拒绝把已撤回版本直接改回生效
        head = backend.store.chain_head(MINOR_ID, Purpose.HEALTH_NOTES)
        with self.assertRaises(InvalidTransition):
            backend.store.append_consent(
                ConsentRecord(
                    minor_id=MINOR_ID, purpose=Purpose.HEALTH_NOTES,
                    version=head.version, form_version="2026-v1",
                    guardian_id=GUARDIAN, status=ConsentStatus.ACTIVE,
                    granted_at=T0, valid_from=T0, valid_until=FAR_FUTURE,
                ),
                expected_head_seq=head.seq,
            )
        final = backend.consents.current_status(MINOR_ID, Purpose.HEALTH_NOTES, now=T0)
        self.assertEqual(final.status, ConsentStatus.WITHDRAWN)
        result = backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.HEALTH_NOTES, now=T0)
        self.assertFalse(result.allowed)

    def test_reads_after_withdraw_always_denied(self) -> None:
        backend = make_backend(grant_purposes=list(Purpose))
        withdrawn = threading.Event()
        results = []

        def withdrawer():
            backend.consents.withdraw(MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN, now=T0)
            withdrawn.set()

        def reader():
            withdrawn.wait(timeout=10)
            results.append(
                backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, Purpose.HEALTH_NOTES, now=T0)
            )

        run_threads([withdrawer] + [reader for _ in range(6)])
        self.assertEqual(len(results), 6)
        self.assertTrue(all(not r.allowed for r in results))
        self.assertTrue(all(r.reason == "consent_withdrawn" for r in results))

    def test_mixed_concurrent_ops_keep_chain_consistent(self) -> None:
        backend = make_backend()

        def worker(purpose, n):
            for i in range(n):
                try:
                    backend.consents.grant(
                        MINOR_ID, purpose, GUARDIAN,
                        form_version=f"f-{threading.get_ident()}-{i}",
                        valid_until=FAR_FUTURE, now=T0,
                    )
                except (ConcurrencyConflict, PermissionDenied):
                    continue
                try:
                    backend.consents.withdraw(MINOR_ID, purpose, GUARDIAN, now=T0)
                except ConsentNotActive:
                    pass

        def reader():
            for _ in range(20):
                for purpose in Purpose:
                    backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, purpose, now=T0)

        run_threads(
            [lambda p=p: worker(p, 10) for p in Purpose]
            + [reader for _ in range(3)]
        )
        for purpose in Purpose:
            chain = backend.store.consent_chain(MINOR_ID, purpose)
            self.assert_chain_valid(chain)
            head = chain[-1]
            result = backend.access.read_fields(OPERATOR, Role.OPERATOR, MINOR_ID, purpose, now=T0)
            self.assertEqual(result.allowed, head.status == ConsentStatus.ACTIVE)

    def assert_chain_valid(self, chain) -> None:
        self.assertEqual(chain[0].version, 1)
        self.assertEqual(chain[0].status, ConsentStatus.ACTIVE)
        seqs = [r.seq for r in chain]
        self.assertEqual(seqs, sorted(seqs))
        for prev, cur in zip(chain, chain[1:]):
            if cur.version == prev.version:
                self.assertEqual(prev.status, ConsentStatus.ACTIVE)
                self.assertIn(cur.status, TERMINAL_STATUSES)
            elif cur.version == prev.version + 1:
                self.assertEqual(cur.status, ConsentStatus.ACTIVE)
                self.assertIn(prev.status, TERMINAL_STATUSES)
            else:
                self.fail(f"版本号跳跃：{prev.version} -> {cur.version}")

    def test_guardianship_change_never_leaves_old_guardian_consent_active(self) -> None:
        backend = make_backend(grant_purposes=[Purpose.HEALTH_NOTES])
        changed = threading.Event()

        def granter(i):
            for n in range(15):
                if changed.is_set():
                    break
                try:
                    backend.consents.grant(
                        MINOR_ID, Purpose.HEALTH_NOTES, GUARDIAN,
                        form_version=f"g{i}-{n}", valid_until=FAR_FUTURE, now=T0,
                    )
                except (PermissionDenied, ConcurrencyConflict):
                    break

        def changer():
            backend.guardianships.change(MINOR_ID, NEW_GUARDIAN, actor_id="admin", now=T0)
            changed.set()

        run_threads([lambda i=i: granter(i) for i in range(4)] + [changer])
        head = backend.consents.current_status(MINOR_ID, Purpose.HEALTH_NOTES, now=T0)
        # 旧监护人签署的同意要么已被作废，要么根本不存在；绝不残留生效记录。
        self.assertIsNotNone(head)
        if head.status == ConsentStatus.ACTIVE:
            self.assertEqual(head.guardian_id, NEW_GUARDIAN)
        else:
            self.assertIn(head.status, TERMINAL_STATUSES)
        self.assertEqual(
            backend.store.current_guardian_id(MINOR_ID, T0), NEW_GUARDIAN
        )


if __name__ == "__main__":
    unittest.main()
