"""演示：从“旧同意书撤回后无关岗位仍能查看”到闭环治理。

场景还原：
1. 运营员为未成年人建立三个用途的授权并录入敏感资料；
2. 家长撤回“服务照片”的媒体使用授权（部分撤回）；
3. 无服务关系的运营员、志愿者尝试读取 → 全部拒绝并留痕；
4. 家长通过 API 查看授权去向（含每一次允许与拒绝）；
5. 并发撤回与续期竞争，验证已撤销权限不会复活。

运行：python3 tools/demo_scenario.py
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import BASE, GUARDIAN, MANAGER, MINOR, OP, OP2, VOLUNTEER, make_api  # noqa: E402

from minor_consent import AccessDeniedError, ConcurrencyError, policies  # noqa: E402


def main() -> None:
    api, _ = make_api()

    print("== 1. 家长部分撤回：照片不再用于媒体通稿 ==")
    consent = api.store.get_consent(MINOR, policies.PURPOSE_PHOTOS)
    result = api.parent_withdraw(
        GUARDIAN, MINOR, policies.PURPOSE_PHOTOS,
        {"photo_media_release"}, expected_version=consent.version,
        reason="不同意媒体使用",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print("\n== 2. 无关岗位读取被拒（决定写入审计） ==")
    api.start_service(OP, "assign-vol", MINOR, VOLUNTEER.actor_id, "svc-2026-10")
    for actor, purpose, fields, task in [
        (VOLUNTEER, policies.PURPOSE_EMERGENCY, {"contact_name"}, "incident_response"),
        (OP2, policies.PURPOSE_EMERGENCY, {"contact_phone"}, "incident_response"),
        (OP, policies.PURPOSE_PHOTOS, {"photo_media_release"}, "press_release"),
        (MANAGER, policies.PURPOSE_HEALTH, {"medications"}, "meal_planning"),
    ]:
        try:
            api.read_sensitive_fields(actor, MINOR, purpose, fields, task)
            print(f"  {actor.actor_id}: 意外放行！")
        except AccessDeniedError as exc:
            print(f"  {actor.actor_id}({actor.role.value}) 读取 {sorted(fields)} -> 拒绝[{exc.reason}]")

    print("\n== 3. 家长查看授权去向（不含原值） ==")
    disclosure = api.parent_view_disclosure(GUARDIAN, MINOR)
    for event in disclosure["events"][-6:]:
        print(f"  #{event['seq']} {event['actor_id']} {event['action']} "
              f"{event['purpose']} {event['fields']} -> {event['decision']}[{event['reason']}]")

    print("\n== 4. 并发撤回 vs 续期：已撤销权限不复活 ==")
    consent = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
    barrier = threading.Barrier(3)
    outcomes: dict[str, str] = {}

    def withdraw() -> None:
        barrier.wait()
        try:
            api.parent_withdraw(GUARDIAN, MINOR, policies.PURPOSE_EMERGENCY,
                                None, expected_version=consent.version)
            outcomes["withdraw"] = "ok"
        except ConcurrencyError:
            outcomes["withdraw"] = "conflict"

    def renew() -> None:
        barrier.wait()
        try:
            api.renew_consent(OP, MINOR, policies.PURPOSE_EMERGENCY,
                              BASE + timedelta(days=90),
                              expected_version=consent.version)
            outcomes["renew"] = "ok"
        except ConcurrencyError:
            outcomes["renew"] = "conflict"

    threads = [threading.Thread(target=withdraw), threading.Thread(target=renew)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()
    final = api.store.get_consent(MINOR, policies.PURPOSE_EMERGENCY)
    print(f"  竞争结果: {outcomes}; 最终有效字段: {sorted(final.effective_fields)}")
    if outcomes["withdraw"] == "conflict":
        # 客户端按最新版本重试撤回——撤销意愿不会因并发而丢失。
        retry = api.parent_withdraw(GUARDIAN, MINOR, policies.PURPOSE_EMERGENCY,
                                    None, expected_version=final.version)
        print(f"  按最新版本重试撤回 -> 有效字段: {retry['effective_fields']}")

    print(f"\n审计链完整: {api.store.audit.verify_chain()}, 共 {len(api.store.audit.entries())} 条")


if __name__ == "__main__":
    main()
