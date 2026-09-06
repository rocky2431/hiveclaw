---
document_id: weekend-rc-2026-08-30-index
owner: Example Owner / Codex
status: active
authority: canonical-index
last_reviewed: 2026-09-07
source_commit: 17fed53097457cb23e52bb6545752198926c93ed
verification_status: p01-negative-workspace-path-governance-order-local-review-accepted
---

# Weekend RC 2026-08-30 验收文档组

本目录是本轮整体验修的**唯一导航入口**。它不是另一本总纲；每类事实只由一个文件负责，索引只建立关系，不复制正文。

## 恢复顺序

1. 先读本页，确认文件权威与写入路由。
2. 再读 [03-current-status.md](03-current-status.md)，取得当前目标、提交、阻塞、下一动作和明确未完成项。
3. 按 [04-journey-ledger.md](04-journey-ledger.md) 选择当前旅程，再进入对应领域清单。
4. 发现问题写入 [05-findings.md](05-findings.md)；实际运行结果写入 [evidence/](evidence/README.md)。
5. 历史长账只在 [archive/](archive/README.md) 中查阅，不得反向覆盖当前状态。

自动恢复入口固定为 `03-current-status.md`；本页是人类与 Agent 的文档地图。两者职责不同，不构成双事实源。

`03-current-status.md` 是唯一可变的恢复状态文件，不是唯一需要更新的 canonical 文件。Journey、Finding 和真实运行结果仍分别按本页路由写入既有 ledger、findings 与 evidence；禁止的是另建第二套 state system、shadow ledger 或 semantic controller。

## 权威地图

| 文件 | 唯一职责 | 更新节奏 |
|---|---|---|
| [01-north-star-and-boundaries.md](01-north-star-and-boundaries.md) | North Star、主指标、护栏、范围和产品边界 | 低频；产品裁决变化时 |
| [02-owner-decisions.md](02-owner-decisions.md) | owner 明确接受、拒绝或待决的决定 | 每次 owner 裁决后 |
| [03-current-status.md](03-current-status.md) | 当前提交、生产基线、阻塞、下一动作、Not Done | 每个证据批次或计划步骤后 |
| [04-journey-ledger.md](04-journey-ledger.md) | 验收分母、旅程状态、领域与最新证据关系 | 旅程冻结或状态变化时 |
| [05-findings.md](05-findings.md) | 当前有效 finding、严重度、最早错误状态和修复闭环 | 复现、修复、复验时 |
| [06-runbook-and-release-gates.md](06-runbook-and-release-gates.md) | 执行顺序、测试、部署、停止与发布门 | 执行合同变化时 |
| [domains/](domains/single-agent-and-session.md) | 各能力的验收标准；不记录运行结果 | 产品契约变化时 |
| [evidence/](evidence/README.md) | exact commit / persona / run 的不可变证据记录 | 每次实际运行后新增 |
| [archive/](archive/README.md) | 旧总账、历史推理和过时状态 | 只归档，不作为当前权威 |

## 领域清单

- [Single Agent 与 Session](domains/single-agent-and-session.md)
- [Memory、Knowledge 与 Growth](domains/memory-knowledge-and-growth.md)
- [HR、身份与权限](domains/hr-identity-and-permissions.md)
- [协作、Workflow 与 A2A](domains/collaboration-workflow-and-a2a.md)
- [Automation、Hook 与外部能力](domains/automation-hooks-and-capabilities.md)
- [前端与产品消费](domains/frontend-and-product-consumption.md)

相关审计：[CC+ 产品目标、系统差距与代码精简审计（2026-09-05）](../../ccplus-product-and-code-audit-2026-09-05.md)。该文是评估快照与建议，不替代本组目标、状态、Findings 或发布 verdict。

## 机器清单边界

现有 [`acceptance/atomic_user_journeys.v1.json`](../../../acceptance/atomic_user_journeys.v1.json) 是被后端架构测试和 Playwright 消费的 15 条确定性 CI 旅程，允许声明过的受控外部 fake。它是 CI 行为底线，不是本轮生产 NPTCR 分母。

本轮生产分母已按 owner 裁决冻结在 [`acceptance/weekend_production_journeys.v1.json`](../../../acceptance/weekend_production_journeys.v1.json)：35 个候选组展开为 96 条可独立计分旅程，禁止 external fake。当前 manifest 的 `P01-MAIN` 已有两次clean signed-in pass，但authority-negative返回治理依赖`unavailable`而不是typed denial，cleanup也未运行；因此仍没有 `Closed loop` 旅程，NPTCR 为 0%，且新应用修正后旧双遍不能迁移。旧 manifest hash 上的 `P29-PADMIN` pass 1 只保留为历史 supporting evidence。[`backend/scripts/weekend_rc_gate.py`](../../../backend/scripts/weekend_rc_gate.py) 只校验 exact manifest/evidence/deployment facts并计算机械分数，固定输出 `semantic_verdict=not_computed_by_tool`。

`WORKSPACE-PATH-GOVERNANCE-ORDER-001`的共享pre-governance最小候选已完成zCode GLM-5.3作者修正、CC独立接受与Codex独立红/绿及邻接复核；production仍是未含该候选的exact `17fed530`，只有后继exact CI、三服务同源部署和新D真实P01重跑才能改变上述Breakpoint。

```bash
python3 backend/scripts/weekend_rc_gate.py validate
python3 backend/scripts/weekend_rc_gate.py score --deployed-commit <40-char-application-sha>
```

## 执行队列边界

- Codex Goal 只保存最终目标和停止条件；本目录与冻结 manifest 记录 owner 接受的验收合同。Hive 产品 turn 的 selected runtime LLM 负责任务语义，RC 循环的主 Codex负责解释真实证据并形成验收 verdict，owner 负责产品/风险裁决；文档和机器清单都不能机械地产生语义结论。
- GitHub Issue 只是从 fresh finding 投影出的 bounded work packet；label、comment、assignee、open/closed 都不是 Journey/Finding verdict。
- 2026-09-04 的 PDEC-012 替代旧执行分工：zCode 负责后端及功能实现，Kimi Code 负责前端 UI；主 Codex 随后独立检查代码和证据、核对结论并补充遗漏。2026-09-05 PDEC-014 进一步规定 CC 不可用时不得等待：优先由未参与该候选实现的 zCode/Kimi 交叉只读 review，Codex 加严源码、调用链、逆向红例与真实运行证据复核；重大节点仍做方案/反例/证据对账，但不绑定 CC 品牌。
- 主 Codex 保留 Goal、派单、集成、生产 E2E/A2A、部署、最终验收和交付权威；通过现有 `agent-delegation` 派发有边界的任务，不新增 controller 或账本。worker/reviewer 的独立判断是审查意见，不直接改写 Journey verdict。
- 代码事实由 Git diff、live wiring 和测试证明；生产事实只能进入不可变 evidence。非作者交叉审查与 Codex 严格复核前，不得因 worker、Issue、PR 或 CI 显示成功而升级状态；作者自审不算独立意见。
- 每个已复现根因使用一个可独立回滚的 Codex integration commit；不为每个 checklist/test/receipt 建 commit，也不在每次 push 后自动部署。
- 最终应用提交 `D` 同时部署三个 Railway 服务；随后纯证据提交 `E` 记录在 `D` 上完成的生产双遍。`E` 不重新部署，避免证据提交产生新的未验应用身份。

## 写入路由

| 要写的事实 | 只能写到 |
|---|---|
| 改变目标、范围、角色或 UI 基准 | `01-north-star-and-boundaries.md`，并在 `02-owner-decisions.md` 记录裁决 |
| 当前做到了哪里、下一步做什么 | `03-current-status.md` |
| 新增、删除、排除旅程 | `04-journey-ledger.md`；冻结后同步 production manifest |
| 新缺陷或 blocker | `05-findings.md` |
| 改验收标准 | 对应 `domains/*.md` |
| 命令、测试、部署和恢复顺序 | `06-runbook-and-release-gates.md` |
| 某次真实结果 | 新建 `evidence/<exact-commit>/<journey>-pass-N.md` |
| 旧日志或已被取代的长说明 | `archive/` |

## 防漂移规则

1. `Closed loop` 必须链接到同一生产提交的有效证据；测试绿、部署成功和产品验收分别记账。
2. Evidence 文件新增后不改写；纠错时新增 correction 文件并声明 supersedes。
3. Active 文档不粘贴 raw log、完整 payload 或大段命令输出，只保存结论、精确命令、结果和 artifact reference。
4. 现有 `J-01`～`J-15` 不重编号；生产旅程使用独立命名空间，冻结时显式映射 CI 基线。
5. 索引和 status 可以汇总，但不得成为验收定义、finding 或证据的第二权威。
6. 结构检查只验证文件、ID、链接、字段和 `Closed` 证据引用，不判断语义质量。
7. Goal、Issue、worker 回执和 Git 状态是不同事实源，不互相推导；只有 Codex 按七原子和真实旅程更新 canonical verdict。

## 可读性预算

- 本索引目标不超过 160 行。
- `03-current-status.md` 目标不超过 220 行。
- 其他单个 active 文档不超过 500 行；超过时按用户旅程拆分，不在原文件继续堆叠。
- 历史 archive 不受行数限制，但不得绑定为恢复入口。

## 上位权威

本目录服从仓库 `AGENTS.md`、`docs/hive-sota-master-goal.md`、`docs/ccplus-north-star-contract-2026-06-24.md` 和当前 frontend design authority。若产品语义冲突，以当前 owner 裁决和 `AGENTS.md` 的产品目标及 hard invariants 核对；不能自行裁决的范围或风险变化回到 owner。本目录只负责本轮 Release Candidate 的范围、执行与证明。
