---
document_id: weekend-rc-2026-08-30-journey-ledger
owner: Example Owner / Codex
status: active
authority: canonical-human-journey-ledger
last_reviewed: 2026-08-31
source_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
verification_status: frozen-96-current-blocker-scope-aligned-no-current-manifest-pass
---

# Journey Ledger

[返回索引](README.md) · [当前状态](03-current-status.md) · [Runbook](06-runbook-and-release-gates.md)

本文件记录旅程分母、主 Codex/owner 已接受的闭环状态和证据链接。Domain 文档记录验收标准；Evidence 文件记录实际结果；本文件不复制两者正文，也不从机械字段自行推导语义 verdict。

## 分母状态

- 当前：`Frozen`，共 **96** 条可独立计分的 production journeys。
- 机器权威：[`acceptance/weekend_production_journeys.v1.json`](../../../acceptance/weekend_production_journeys.v1.json)，freeze basis `c18b181c690fe3c4aa5366a8fd504023b0c41864`；记录 persona、entry、data version、allowed effects、acceptance、fault probes、evidence path 和 cleanup。
- 冻结后不得删除或合并失败项；owner 只能带理由标为 `Excluded`。
- 只有 unresolved product-controlled requirement 可记录 blocking fact `BLOCKED_PRECONDITION`；underlying Journey 保持 `Breakpoint` 或 `Missing`，留在分母并按未闭环计。可恢复的合成 fixture、仓库 runtime/adapter 和已验证第三方 external readiness 不得冒充该 fact，也不得用 fake、历史 PASS 或未执行状态替代。
- production release 要求全部 in-scope 冻结旅程在同一 exact commit 连续两遍 clean pass；owner 带理由明确 `Excluded` 的旅程不进入 NPTCR 分母，组级通过不能替代子旅程。

## 现有确定性 CI 基线

来源：[`acceptance/atomic_user_journeys.v1.json`](../../../acceptance/atomic_user_journeys.v1.json)。下表只是映射；是否当前通过必须以重新运行结果为准。

| ID | CI 旅程 | 对应领域 | 当前用途 |
|---|---|---|---|
| J-01 | message_to_terminal_answer | Single Agent / Session | deterministic CI floor |
| J-02 | upload_to_deliverable | Frontend / Artifact | deterministic CI floor |
| J-03 | plan_confirm_to_observation | Single Agent / Plan | deterministic CI floor |
| J-04 | goal_long_task | Single Agent / Goal | deterministic CI floor |
| J-05 | schedule_trigger_delivery | Automation | deterministic CI floor |
| J-06 | branch_fork_rewind | Session recovery | deterministic CI floor |
| J-07 | personal_knowledge_ingest_search | Knowledge | deterministic CI floor |
| J-08 | skill_discover_load_evolve | Growth / Capability | deterministic CI floor |
| J-09 | spawn_subagent | Collaboration | deterministic CI floor |
| J-10 | agent_team_aggregate | Collaboration | deterministic CI floor |
| J-11 | dynamic_workflow | Workflow | deterministic CI floor |
| J-12 | hr_confirm_provision | HR | deterministic CI floor |
| J-13 | channel_ingress_delivery | Channel | deterministic CI floor |
| J-14 | local_agent_bridge | Local Agent | deterministic CI floor |
| J-15 | operator_inspector_audience | Frontend / Audience | deterministic CI floor |

该 manifest 声明 `llm_provider`、`channel_provider`、`sandbox_provider`、`local_bridge_peer` 为 external fakes；因此 15/15 绿不能计为 production NPTCR。

## Production journey 候选组

| Candidate ID | 旅程组 | Domain 权威 | 分母状态 | 当前闭环判断 |
|---|---|---|---|---|
| PJ-01 | 单 Agent 真实开放任务与 CCPlus 生命周期 | [Single Agent](domains/single-agent-and-session.md) | Frozen ×1 | Partial loop |
| PJ-02 | Session streaming、terminal、failure、reload 同构 | [Single Agent](domains/single-agent-and-session.md) | Frozen ×1 | Partial loop；核心子集有历史 Closed 证据 |
| PJ-03 | 20 条斜杠命令逐条产品闭环 | [Single Agent](domains/single-agent-and-session.md) | Frozen ×20 | Breakpoint |
| PJ-04 | Plan / Goal / Task / Ledger | [Single Agent](domains/single-agent-and-session.md) | Frozen ×3 | Partial loop |
| PJ-05 | J1 candidate provisional trial | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-06 | J2 longitudinal growth 与 owner feedback | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-07 | J3 platform change non-regression | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-08 | J4 FreeCode/Hermes real bakeoff | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | `Breakpoint / IMPLEMENTATION_QUEUED`：构建 Hive/FreeCode same-envelope adapter；旧 [preflight](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/P08-J4-blocked-runtime-contract.md) 只保留历史事实 |
| PJ-09 | Agent Memory T0→T2→T3→Soul/Skill reuse | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-10 | Personal KB multi-format ingest/search/read/cite | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×5 | Partial loop |
| PJ-11 | Company KB direct/background import→publish→read | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×2 | Partial loop |
| PJ-12 | Personal/Agent→Company promotion 与治理 | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-13 | HR 创建、revise/reject/confirm/provision/首任务 | [HR/Identity](domains/hr-identity-and-permissions.md) | Frozen ×1 | Partial loop |
| PJ-14 | Agent→HR 受治理 handoff | [HR/Identity](domains/hr-identity-and-permissions.md) | Frozen ×1 | Partial loop |
| PJ-15 | 角色/权限正负向与 active revocation | [HR/Identity](domains/hr-identity-and-permissions.md) | Frozen ×4 | Breakpoint |
| PJ-16 | owner transfer、offboarding、retention/export/delete | [HR/Identity](domains/hr-identity-and-permissions.md) | Frozen ×3 | Partial loop / Missing policies |
| PJ-17 | Sub-agent 完成、失败、取消、父任务消费 | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×1 | Partial loop |
| PJ-18 | Agent Team fanout/review/partial failure/integration | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×1 | Partial loop |
| PJ-19 | Dynamic Workflow preview/confirm/run/wait/resume/result | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×1 | Partial loop |
| PJ-20 | Fixed A2A Workflow version/publish/run/audit | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×1 | Partial loop |
| PJ-21 | A2A sync/async/continuation/nested/artifact/fixed edge | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×6 | Partial loop |
| PJ-22 | once/schedule/bounded loop/event trigger | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×4 | Breakpoint aggregate |
| PJ-23 | Notification/Approval/Channel return loop | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×3 | Breakpoint aggregate |
| PJ-24 | Local Agent pair/online/offline/approval/reconnect/revoke | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×1 | `Breakpoint / RECOVERY_QUEUED`：PDEC-008 已授权 lab login/pair/revoke，真实 bridge/provider secret 仍不可读取或轮换 |
| PJ-25 | Hook blocking/observe-only/lifecycle/recovery | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×3 | Breakpoint aggregate |
| PJ-26 | Skill trust/load/use/update/revoke | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×1 | Breakpoint aggregate |
| PJ-27 | MCP/Connector auth/use/expiry/revoke/schema change | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×1 | Breakpoint aggregate |
| PJ-28 | Agent rail/AgentDetail employee scale and navigation | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×3 | Breakpoint |
| PJ-29 | Employee/admin/platform/operator audience split | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×4 | Breakpoint |
| PJ-30 | Artifact preview/download/version/ACL/reopen | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×4 | Breakpoint aggregate |
| PJ-31 | Async deep-link/inbox/unread/dedupe/expiry | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×1 | Breakpoint aggregate |
| PJ-32 | Theme/narrow screen/keyboard/a11y/state screenshots | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×4 | Breakpoint aggregate |
| PJ-33 | MiniMax/GLM/DeepSeek model fidelity 与资源观测 | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×3 | Breakpoint aggregate；DeepSeek 当前 `EXTERNAL_UNAVAILABLE`，P33-DEEPSEEK 保持未闭环且不伪造 success |
| PJ-34 | Prompt injection、cross-tenant、secret、replay、approval、delegation | [Release Gates](06-runbook-and-release-gates.md) | Frozen ×6 | Breakpoint aggregate |
| PJ-35 | three-service exact deploy、rollback 与 production double pass | [Release Gates](06-runbook-and-release-gates.md) | Frozen ×1 | Partial loop |

## 每条冻结记录必需字段

`journey_id`、persona/principal、真实入口、输入与数据版本、allowed tools/effects、成功硬判据、negative authority、fault/recovery probe、expected artifact、latency/cost measurement、evidence location、cleanup/retention。

## 最新有效证据索引

分母已冻结。旧 manifest hash 上的 `P29-PADMIN` production clean-path pass 1 只保留为 historical supporting evidence；current manifest 下 pass 1/pass 2 均未运行，不计入 NPTCR。这里只登记关系，不复制证据正文：

latest exact `bf94b76a` finding verification 为 [`PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001`](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001-production-verification.md) 与 [`SYSTEM-SETTING-SECRET-DISCLOSURE-001`](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/SYSTEM-SETTING-SECRET-DISCLOSURE-001-production-verification.md)。旧 [`P29-PADMIN-pass-1`](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/P29-PADMIN-pass-1.md) 绑定 manifest `d320edce…`，不能迁移为 current-manifest PASS；旧 [`BLOCKED_PRECONDITION`](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/P29-PADMIN-fault-pass-2-role-session-precondition.md) 文件也只保存当时缺身份的历史事实。PDEC-008 后当前状态是 supported-path fixture setup pending，current-manifest canonical pass 1/pass 2 均未运行。

| Journey | Pass 1 | Pass 2 | Fault/Recovery | Negative Authority | Final Verdict |
|---|---|---|---|---|---|
| P01-MAIN | [`3c920534` attempt](evidence/3c92053466b26e872c21a7c7e0b50d37ae6342ea/P01-MAIN-pass-1.md) 为 `Breakpoint`：员工任务/UI reload/artifact成功，但canonical terminal outbox dead-letter，故不计pass | 未运行 | 共享有序排空修复待exact部署；旧dead letter仍需一次exact operator redrive，尚未执行 | 未运行 | `Breakpoint`，未 Closed |
| P29-PADMIN | 未运行；旧 `d320edce…` pass 1 仅历史 supporting evidence | 未运行；supported-path fixture setup pending | 旧 denied-route/reload evidence retained；current-manifest expired-session/role-change 待测 | 旧 9 URL + 14 API evidence retained；current-manifest 待测 | `Partial loop`，未 Closed |
| 其余 94 条 | — | — | — | — | 未执行或仅有 finding-level evidence |
| Aggregate | 0 次 current-manifest pass | 0 次 current-manifest 双遍 | — | — | 0/96 Closed；NPTCR 0% |

## 状态变化规则

1. Domain 标准存在不等于旅程存在。
2. Manifest 冻结不等于执行通过。
3. 自动化绿不等于 production pass。
4. 单次 pass 不等于双遍 `Closed loop`。
5. `Closed loop` 必须链接 exact commit 下的 pass 1、pass 2、fault/recovery 和 authority-negative evidence。
