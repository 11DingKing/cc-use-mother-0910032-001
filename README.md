# 未成年人隐私同意

本项目维护未成年人隐私同意的领域约定、角色边界与样例数据，并提供完整的 Python 后端实现：按数据用途维护监护人同意版本、有效期与可见角色，读取敏感字段时同时校验服务关系与最小必要范围，访问决定写入审计且不泄露原值。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/minor_consent/`：后端实现（见下）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/demo_scenario.py`：家长投诉场景的闭环演示。
- `tests/`：契约回归与后端行为测试（含并发安全）。

## 后端结构（`src/minor_consent/`）

- `policies.py`：用途化策略表——紧急联系人、健康注意事项、服务照片授权三个用途的字段清单、字段级可见角色、任务最小必要范围与留档期限。
- `models.py`：角色、同意状态机（草拟→待核验→已确认→执行中→已归档）、不可变同意版本、监护关系、服务关系、审计条目。
- `store.py`：线程安全存储；同意版本 CAS（`expected_version` 乐观并发），历史版本只追加不改写。
- `consent_service.py`：起草/提交/确认/撤回（支持部分撤回）/续期/归档；撤回单调生效，只有监护人显式起草并确认新版本才能重新授权。
- `access_service.py`：字段读取三重校验（有效同意 → 进行中服务关系 → 可见角色与最小必要范围），每次决定（允许/拒绝）写入审计；已归档记录仅场馆负责人可走合规通道。
- `guardianship_service.py`：监护关系变更即挂起全部未归档同意，旧监护人权限立即失效，新监护人逐项重新核验。
- `export_service.py`：监护人数据主体导出（含封存标注、版本史、授权去向）；留档期满销毁原值仅留审计与墓碑。
- `audit.py`：只追加、哈希链防篡改的审计日志，绝不记录字段原值。
- `api.py`：无框架依赖的门面，家长可查看授权现状与授权去向、发起导出、撤回授权；变更类操作均要求 `expected_version`，并发更新不会恢复已撤销权限。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

场景演示：`python3 tools/demo_scenario.py`
