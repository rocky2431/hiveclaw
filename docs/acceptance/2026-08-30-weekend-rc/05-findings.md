---
document_id: weekend-rc-2026-08-30-findings
owner: Codex
status: active
authority: canonical-active-finding-ledger
last_reviewed: 2026-09-07
source_commit: 17fed53097457cb23e52bb6545752198926c93ed
verification_status: p01-negative-workspace-path-governance-order-local-review-accepted
---

# 当前 Findings 与 Blockers

[返回索引](README.md) · [当前状态](03-current-status.md) · [Journey Ledger](04-journey-ledger.md)

历史包、旧 PASS 和已被取代的根因只保留在 [archive](archive/README.md)。本文件只接纳当前仍需处理的 finding；旧账内容若没有 fresh reproduction，不自动成为当前缺陷。

**2026-09-05 PDEC-013 改变管理员业务访问合同。** 下列 `PLATFORM-ADMIN-BUSINESS-BODY-001`、`PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001` 及相关 private-deny 记录中的 `Verified` 只说明历史合同当时实现，不要求继续拒绝管理员业务访问。新合同差异统一由 `ROLE-CONTRACT-ALIGNMENT-001` 跟踪；明文凭据保护、无权限 principal 的 denied/cache recovery、跨公司员工隔离和 delegated manage 不得扩权等有效修复继续保留。旧 evidence 不修改、不迁移为新 PASS。

## Finding 状态

`Observed` → `Reproduced` → `Fix Candidate` → `Verified` → `Closed`。Review 失败使用 `Review Failed`；确认不属于范围且不是现有契约缺陷时才用 `Excluded`。

只有 `Reproduced` 且已记录最早错误状态的 finding 才能生成修复 Issue。Issue 必须回链本文件的 finding ID 和冻结 Journey ID；worker 回执、PR、CI 或 Issue closed 都不能自动推进 finding 状态。

## 当前 P1/P2 findings

### 当前状态

| ID | 状态 | Severity | Journey | 最早错误状态 | 当前根因边界 | 下一动作 |
|---|---|---:|---|---|---|---|
| WORKSPACE-PATH-GOVERNANCE-ORDER-001 | Fix candidate / local review accepted / production reproduced | P1 | P01-MAIN negative / shared file tools | CEDAR R2对`../P01-MAIN-NEGATIVE-CEDAR-ELM-20260907.md`唯一一次`write_file`零效果，但返回`governance_dependency_unavailable`/`unavailable`/retryable，而不是要求的non-retryable typed denial；invocation无fence且保持`prepared_not_started`，随后允许workspace写读正常 | 标准Session的`run_tool_execution`只为`session_exact_scope` profile提前拒绝路径；普通Session在既有`authorize_workspace_tool_path`最终I/O检查前先运行整条可超时governance。最窄候选在可信参数改写、hooks/assets之后且governance/pre-effect之前复用同一权威，覆盖direct write/edit/delete、`fs_write`及Office create/apply，最终handler guard保留且不改5秒阈值。zCode GLM-5.3最终hash=`93bf151d…21a6`/`ada8ef81…8473`；CC先阻断漏掉的CORE facade/Office与whitespace parity，最终`e0ed570c…`接受。Codex独立关闭新stage精确恢复`unavailable/retryable`、开启后得到`denied/non-retryable`，并通过focused30、tools700、RLS登记2、结构10及Ruff/format/diff-check | 将两份backend代码/测试与task-owned文档/evidence形成后继application；通过同head exact CI和clean archive三服务部署后，在新D重跑P01双遍、negative与cleanup。当前production仍为Breakpoint，不迁移旧双遍 |
| TRANSCRIPT-PROJECTION-TERMINAL-DEADLETTER-001 | Verified / P01 double-pass consumed | P1 | P01-MAIN / P02-STREAM | completed employee run产生1033条transcript events；约12分钟后仍有118条pending，5秒sweeper约每轮只推进一个frontier；required terminal outbox在attempt 8先以`WebTerminalBoundaryPending`进入dead letter，RuntimeTask `completion_outbox_settled_at`保持null | 共享有序排空修正经zCode、CC `4fc7c994…`与Codex红/绿接受并在exact `53e23d1a`发布；旧1033条最终全projected。后继exact `17fed530` fresh pass1/pass2分别产生1461/1617条transcript并全部projected，两条新terminal outbox均attempt1自然delivered，证明大prefix正常路径已双遍真实消费。`completion_outbox_settled_at`不适用于`web_chat_turn`，不再用作该类型settlement信号 | 保留共享修正；P02 streaming继续覆盖后再决定Closed，不把下方独立idle-seal根因混回本finding |
| T0-IDLE-SEAL-TERMINAL-RECOVERY-001 | Verified / production recovery and P01 double-pass consumed | P1 | P01-MAIN / P02-STREAM | exact `53e23d1a`部署后对`7b200f1c…`唯一一次operator redrive为HTTP 200/audit `df70806a…`，worker仍在attempt9以`WebTerminalBoundaryPending`dead-letter，required boundary未delivery | zCode `ff19df81…`删除alias并补真实projector held/failed回归；CC `bdb19fa5…`与Codex独立接受。exact `17fed530` Harness `34046037891`三job全绿并三服务同源部署；唯一attempt10新增单audit后delivered，复用原idle T0 event/seq1034且T0 index、transcript、tools、models、artifact、input/final零新增，T2仍held。后继fresh P01双遍新terminal均在attempt1自然delivered | 保留修正；继续P02 streaming，未完成各旅程双遍/negative/cleanup前finding级Verified不得升级Journey Closed |
| HR-MODEL-BOOTSTRAP-001 | Configuration recovered / historical failure retained | P1 precondition | P13-HR / P01-MAIN | CEDAR R2 从首页“创建第一个数字员工”进入 fresh HR Session，唯一输入被接受后立即终止为“当前 Agent 尚未配置模型”，UI 标记不可重试 | owner 已在 selected fixture 正式 UI 直接配置 `zhipu/glm-5.3`；模型池回读启用/默认，HR selector 离开再返回仍回读同一模型。Codex未读取、接收或复制 API key；原 Session 保持失败且未重试 | 该 fixture 配置缺口已恢复，不再以此阻断；managed-model/BYOK 是否为新公司产品合同仍是独立 owner 决策，不影响下方 exact provider billing 事实 |
| HR-MODEL-PROVIDER-BILLING-001 | Recovered / historical EXTERNAL_UNAVAILABLE retained | P1 precondition | P13-HR / P01-MAIN | 已绑定模型后的 fresh HR Session `8544a582…` 确认运行 GLM-5.3，约2分37秒后失败为“模型额度或余额不足”，UI 标记可重试 | 当前失败 run 未复用；随后 fresh Session `3b0b01e8…` 经同一 GLM-5.3 正常完成6步、canonical blueprint经owner授权确认并成功provisioning，证明当前provider readiness已恢复。原错误仍作为当时外部额度事实保留，不改写成Hive defect | 该外部前置已恢复，不阻断当前P01 terminal修正；HR确认/provisioning证据不迁移Journey PASS |
| EXTERNAL-PRINCIPAL-UNBOUND-UNLINK-001 | Fix Candidate / local major review complete | P2 | P27-LOCAL / cleanup | `unlink_external_principal`对从未绑定的principal令`previous=None`，而未绑定ChannelConfig的`self_identity_user_id`也为None；相等分支误把正常Feishu频道标成`identity_rebind_required`并取消配置 | 共享unlink现要求`previous is not None`才触发self-identity失效；作者逆向红例、CC重大挑战及Codex真实PG均确认unbound no-op与bound unlink语义。最终M0审查已闭合 | 保持候选进入coherent D；未部署、未做production cleanup前不记Verified/Journey PASS，不新增状态机 |
| RLS-BYPASS-NESTED-RESTORE-001 | Fix Candidate / local major review complete | P2 | P27-LOCAL / P29-PADMIN | 外层`enter_rls_bypass`内调用`_pairing_identity_is_live`的内层bypass；内层退出曾把GUC恢复为空串而session.info仍为`BYPASS` | 共享contextmanager现恢复实际进入前session scope，并覆盖嵌套BYPASS与tenant/empty/failed-transaction语义；作者、CC与Codex真实PG/RLS证据已在M0重大节点对账闭合 | 保持候选进入coherent D；未部署、未做production RLS负向前不记Verified/Journey PASS |
| TENANT-RETIREMENT-ZOMBIE-AGENT-001 | Fix Candidate / local major review complete | P1 | cleanup / P29-PADMIN / P32-AGENT | 无body公司删除、独立管理/权限更新、已加载Runtime、后台task与channel claim后停用曾让inactive Tenant继续产生业务效果 | 14路径根因修正覆盖真实无body消费、管理/权限/Runtime/task claim；最终5路径补共享channel dispatch DB liveness与durable defer，并保留原`available_at`顺序。CC纠正Tenant表实际ENABLE+FORCE RLS；Codex最终独立8 gates、16 inbox/dispatcher、17 RLS及5hash全绿，M0本地重大节点闭合 | 保持全部候选进入coherent D；未部署、未做production停用/恢复/零效果验证前不记Verified/Journey PASS，PDEC-013不得破坏这些门 |
| ROLE-CONTRACT-ALIGNMENT-001 | Fix Candidate / local cross-layer major review complete | P1 | P15-ADMIN / P15-OPERATOR / P29-CADMIN / P29-PADMIN / P29-OPER / P13-HR | 2026-09-05 owner 明确三角色及管理员私有业务访问；首轮候选虽补主要Session/File/KB/HR/Local Agent路径，但platform-admin仍可不经已认证公司选择、仅凭Agent UUID或`tools.py?tenant_id`跨公司取得业务作用域 | 后端zCode根因修正、CC接受与Codex独立复核成立，当前定向真实PG/HTTP/RLS 70/70。Kimi首稿经CC与Codex阻断inactive公司、HR错误归因、无效选择冷启动、跨tab清空回退及零公司文案后完成窄修；最终Kimi报告`d2dacc32…dbb7`、zCode非作者接受报告`04df7b8d…579a0`。Codex独立5/5恢复反例、12/12角色E2E、21/21单测与tsc绿，跨层重大节点闭合。仍未提交/部署，不计Journey PASS | 冻结coherent D并在owner明确授权后commit/deploy三服务同一exact commit；再做current manifest双遍、三角色/inspector、offboarding/reload/RLS no-leak与真实后台返回App/HR/私有业务消费，保留96 ID/数量/评分 |
| RLS-BYPASS-CROSS-TENANT-CHANNEL-CONFIG-001 | Fix Candidate / local review complete | P1 | P27-LOCAL / P29-PADMIN / cleanup | tenant retirement 在platform-admin审计bypass中复用`revoke_user_authority`→`unlink_external_principal`；后者按tenant锁principal后，用裸`channel_config_id`读取并改写config，未复核config tenant | zCode`94808`在mutation前加载ChannelConfig并校验`tenant_id`，错配typed fail closed。CC`973e71b7…`独立复跑29 real-PG、17 RLS与Ruff全绿；Codex读码确认guard位于全部mutation之前。精确fingerprint为575 stable、2 removed、2 added；作者所称579是报告算术误差，不影响源码真值 | 保持该共享护栏；与下方ChatMessage sibling一起完成最终作者→CC→Codex及M0重大节点对账。未部署、未完成production权限负向，不迁移Journey PASS |
| RLS-BYPASS-CROSS-TENANT-CHAT-MESSAGE-001 | Fix Candidate / local review complete | P1 | P27-LOCAL / P29-PADMIN / cleanup | 同一解绑流程在审计bypass中同步Session用户时，`ChatMessage` bulk update只按`external_principal_id`过滤，没有tenant谓词 | zCode`73599`在唯一共享bulk update加tenant谓词及真实PG正负回归。CC`62107`逆向还原frozen start并独立mutation为1failed/11passed、候选12passed、lifecycle18、RLS17；Codex读码后独立30passed、RLS17、Ruff及`ed86bc4f…`指纹全绿，无补充blocker | 保持候选，和下方Feishu维护修正一起进入M0重大节点对账；未部署、未做production权限负向，不迁移Journey PASS。无需每消息查询、schema或新框架 |
| RLS-BYPASS-CROSS-TENANT-FEISHU-MAINTENANCE-001 | Fix Candidate / local review complete | P1 | P27-LOCAL / cleanup | 跨租户Feishu维护按裸`feishu_user_id`聚合User，Session规范化又按裸自由字符串`conversation_id` bulk改ChatMessage | zCode tenant分组/加载、前置guard及bulk tenant谓词已由CC frozen RED3/GREEN7和Codex真实PG核对；unique/foreign-user后续修正又经CC`66174`与Codex完整复证。最终service/test/manifest为`5bc74030…`/`2e3b34f8…`/`4d82edd2…`，Codex最终3+9+10+17/Ruff绿 | 保持本地候选进入M0重大节点对账；不改历史migration、不加schema/框架/锁。未部署、不迁移Journey PASS；正式maintenance生产执行与历史数据盘点仍另需授权 |
| FEISHU-MAINTENANCE-UNIQUE-FIELD-ORDER-001 | Fix Candidate / local review complete | P1 | P27-LOCAL / cleanup | 合法同租户duplicate中canonical缺`feishu_open_id`、duplicate持有该全局unique值时，旧维护触发唯一约束冲突并回滚 | zCode在单事务内先保存值、duplicate置空+flush释放唯一索引，再赋canonical；reference move与最终delete保留。CC`66174`独立真实PG旧红/新绿、事务/多duplicate核对；Codex读码并跑最终完整维护9、相邻10、RLS17。测试false-red另以canonical Session一行修正，经CC`63456`与Codex3+9复核 | 保持候选进入重大节点对账；不引入合并框架。未部署/未production执行前不记Verified或Journey PASS |
| FEISHU-NORMALIZATION-FOREIGN-USER-001 | Fix Candidate / local review complete | P1 | P27-LOCAL / cleanup | 旧normalize可把本租户ChatMessage实际改挂到外租户User，并在外租户Session占DB唯一键时整批UniqueViolation | 候选按DB真实`(agent_id, external_conv_id)`唯一键先查，再在任何效果前校验target Session/User tenant；错配pair零效果/零计数。CC`66174`独立覆盖两错配、NULL target、错配+合法同批及双legacy；Codex独立复跑真实PG和RLS。合法目标仍走原merge | 保持候选进入重大节点对账；NULL target保守永久skip与bypass调用前提作为残余披露。未部署前不记Verified或Journey PASS |
| WORKSPACE-BACK-TO-APP-001 | Fix Candidate | P2 | P29-CADMIN / P29-PADMIN / P32-ADMIN | 2026-09-04 production `/enterprise/info` 与 `/enterprise/dashboard` 的 platform-admin 页面只有后台内导航和退出登录，没有返回 App；“返回总览”仍在公司后台 | Kimi复用既有`SurfaceLayout`/`nav.backToApp`；CC`36123b43…`独立确认与AdminLayout同模式且mock E2E可回`/home`、保留localStorage token/company，Codex读源码/图并独立跑33/33 E2E+23聚焦单测。证据仍是合成路由，不是生产登录态 | 保持候选，不退回此子项；后续x64CI与final D production分别用platform/company admin从真实后台返回App，核对认证与合法公司上下文，不用mock值冒充生产证据 |
| SIDEBAR-COLLAPSED-SETTINGS-001 | Fix Candidate | P2 | P28-SMALL / P32-AGENT | 2026-09-04 production 首页先折叠侧栏再点“设置”，按钮 AX 状态变成 expanded，但连续读回没有任何账户/主题/通知选项；实际 1280×720 截图也没有菜单。展开侧栏后同一设置菜单可以正常出现 | Kimi移除折叠`display:none`并用既有rail token放fixed flyout；CC实测6项hit-test全部可达、pointer/keyboard/Escape/outside/route/双语主题绿，Codex核对DOM包含/outside逻辑、截图并独立跑33/33 E2E+23聚焦单测。route后焦点落body是全局NavLink现状，非本增量回归 | 保持候选；后续x64CI及final D production重验折叠/展开、窄屏、真实角色菜单和焦点。全局SPA route-focus另按实际a11y矩阵处理，不在本补丁造新导航框架 |
| HR-ENTRY-ROLE-RECOVERY-001 | Fix Candidate / local major review complete | P2 | P13-HR / P29-PADMIN / P32-AGENT | 2026-09-04 已登录的 platform-admin 从首页/`/agents/new` 点击 HR 创建，仅得到临时重试文案 | PDEC-013后端将合法选定公司接入同一HR流程；前端区分需选公司、无权限和Session阶段失败，不再把任意403伪装成重选公司。Kimi、zCode非作者与Codex独立恢复反例均通过，保留preview/exact confirmation及幂等创建 | 保持候选进入coherent D；部署后以platform/company admin和employee合法身份做直达/reload、创建与首个任务真实双遍。未部署前不记Verified/Journey PASS |
| KNOWLEDGE-NOVICE-DISCLOSURE-001 | Fix Candidate / local review complete | P2 | PJ-10 family / P32-AGENT | 2026-09-04 production `/knowledge` 首屏展示“Owner 级别的一份真相”“文库 canonical MD”“画像 taste / profile”“投喂与管线”；失败图片仅提示“此处不支持该媒体类型”并显示“重试”，没有说明所需前提 | Kimi完成用户语言、真实retry前提、focus与畸形detail降级；CC首审阻断错误grant承诺/窄屏退化/focus no-op后复审通过。Codex继续拒绝800px只露4项及隐藏Grants，最终3×2/2-column 6/6入口、机器值白话投影与`1 part`/`1 document`原生plural已收口；Codex最终独立35/35、i18n4119/build、几何E2E及三图复查全绿 | 保持本地候选进入coherent D；final D在signed-in Personal Knowledge首屏、导入错误恢复、light/dark/800/phone和PDEC-013管理员消费中复验。未部署前不升级Verified/Journey PASS；PL4 list/detail一致性继续按后端真实路径处理 |
| SESSION-NOVICE-DISCLOSURE-001 | Fix Candidate / local review complete | P2 | P32-SESSION / P32-ADMIN / P30-MD | idle/late audience/选择/轨内几何及正文、managed filter/row/action与原生resize角遮挡已有本地候选；Codex另复现740首屏列表只余约18px且三档原生向下拖动均不能增高 | Kimi`89115`用既有CSS-native browser做默认168px、max50dvh并移除118px cap。CC`72849`独立覆盖三宽度和两短高度，首屏完整行、list68、真实drag168→248、rail/overflow与聚焦3、cold/warm25、tsc/Vitest362/i18n/build全绿；Codex读码/图并串行复跑聚焦3、完整25、tsc均绿。冻结hash为`89b00fea…`/`8f584f54…`/`89fe736f…`/`324229e6…` | 保持本地候选；Linux twin必须由x64 CI真实再生成，final D用真实角色/短视口/resize再验。CSS说明注释的“majority”在最大拖动时不精确但行为正确，不为注释重开实现；未部署、不提前写production PASS |
| DANGER-CONFIRM-FOCUS-001 | Fix Candidate / local review complete | P2 | P32-SESSION / destructive actions | 键盘Enter打开删除Session确认后，`ConfirmModal`延迟100ms把焦点移到危险确认按钮；连续Enter可把“打开”升级为删除确认 | Kimi`89115`将danger延迟初始焦点放到Cancel并清timer，非danger仍放Confirm。CC`72849`独立验证100ms前快速第二Enter只回到下层open且零DELETE、延迟第二Enter取消、close/unmount/reopen与非danger；Codex核对真实Session E2E和实现，并独立完整spec25/25、tsc绿 | 保持最小焦点修正，不加第二确认层或状态机；final D在真实删除权限正负向中复验一次效果与取消零效果，未部署前不升级Journey PASS |
| SESSION-AUTHORITY-PRESENTATION-001 | Verified | P1 | P29-PADMIN | backend 对无 operator authority 的跨用户 Session message/lineage 返回 `Session not found`，但旧前端仍显示“完成 / Read-only · User / 1 个步骤 / 运行错误”与完整 Session runtime shell | exact `bbf6d234` 在 authority resolution 前只显示 skeleton；403/404 清除 Session timeline/runtime cache并呈现 truthful denied/not-found，安全返回 `/agents`；5xx/network retry 与合法 Session 消费保持原语义 | 保持 `Verified`；provider health/audit finding 已关闭，仍须完成 P29-PADMIN 其余 API/compliance 正向面、pass-2、role-change/reload 与四角色 screenshot matrix 后才可 `Closed` 或写 P29 PASS |
| RUNTIME-GUARD-PRESENTATION-001 | Verified | P2 | P29-PADMIN | runtime protection heading/badge 显示“被保护的任务 0”，却列出 5 条 `active` run 并称“系统保护机制已介入” | exact `6a6695e8` 让 API 的 active reason 表达正常运行；无 protected run 时 UI 诚实标为“最近运行”，保留 active rows 与暂停能力，真正 protected run 仍优先展示 | 保持 `Verified`；provider health/audit finding 已关闭，P29 其余 API/compliance evidence、fault/reload、pass-2 与四角色 matrix 完成后才可 `Closed` 或写 P29 PASS |
| LLM-PROBE-AUDIT-001 | Verified | P1 | P29-PADMIN / P33-GLM | `/enterprise/llm` Test 发生真实 provider/token/cost effect，但 backend 不写 canonical audit；audit UI 只读 legacy agent-bound log，platform admin selected tenant 也未固定到 canonical audit query | exact `cc6e7262` 在 provider effect 前 durable commit started event，终态 durable commit completed event；effect 后 terminal audit 失败返回 non-retryable typed result；canonical selected-tenant audit 与 legacy log 在 UI 合并消费 | 保持 `Verified`；MiniMax/GLM/DeepSeek 的 bounded health verdict 已记录，但 P33 frozen compatibility tasks、P29 pass-1/pass-2、role/fault/negative matrix 均仍 open，不写 Journey PASS |
| AUDIT-DEFAULT-DISCLOSURE-001 | Verified | P1 | P29-PADMIN | admin audit 默认展开 raw details：production DOM 含 `session_id` 110、`job_id/issues` 各 94、`reason` 41、`agent_name` 77、raw provider error 90；search/CSV/API 也可读 raw payload，export/chain 未固定 selected tenant | exact `b23e9421` 以 server summary schema + CSV/search boundary + frontend allowlist 只暴露 control-plane facts；raw canonical evidence 不改写；list/export/chain 共用 selected-tenant RLS scope | 保持 `Verified`；P29 的 employee/company-admin/operator principals、四角色 screenshot/API matrix、双遍与完整 negative/fault 仍 open，不写 Journey PASS |
| PLATFORM-ADMIN-BUSINESS-BODY-001 | Verified | P1 | P29-PADMIN | `/enterprise/info` 对 platform admin 默认显示公司介绍正文、legacy export 与 broadcast controls；raw info 和 `company_intro*` API 也允许该角色读写业务正文 | exact `8f6a7263` 让 backend raw route 在 authenticated role boundary 返回 403，frontend 不请求/挂载 org-admin content，并把页面描述收敛为 role-appropriate actions；tenant identity/timezone/presentation 保留 | 保持 `Verified`；直接 production API 403 receipt 与 employee/company-admin/operator principals、四角色双遍/完整 fault-negative 仍 open，不写 Journey PASS |
| PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001 | Verified | P1 | P29-PADMIN | exact `8f6a7263` 的 platform admin dashboard/导航展示全部 company-admin surface；直接访问 digital employees、knowledge、users、org、invitations、HR、approvals、guardrails 仍得到业务 DOM 与 200 API | exact `bf94b76a` 以 shared role registry/route guards 和 backend exact-role checks 分离 platform/company workspace；Agent 只保留 ownership 或 exact user scope，不继承 company/department scope | 保持 `Verified`；member/org-admin/operator 三个真实 principal、四角色 screenshot/API matrix、role recovery 与 P29 双遍仍 open，不写 Journey PASS |
| SYSTEM-SETTING-SECRET-DISCLOSURE-001 | Verified | P1 | P29-PADMIN | `/system-settings/feishu_org_sync` 对 platform admin 为 200；GET/PUT 直接返回包含 `app_secret` consumer field 的完整 stored value，generic route 还允许任意 global key | exact `bf94b76a` 在 DB 前执行 role/key allowlist，并把 Feishu GET/PUT response 投影为 `app_secret_configured`；stored value 不改写 | 保持 `Verified`；当前无 signed-in org-admin 可做 production 200 projection screenshot，四角色矩阵/P29 双遍仍 open；不读取或改写真实 credential |
| IDENTITY-FIXTURE-BOOTSTRAP-001 | Verified | P1 | P29-CADMIN / P29-EMPLOYEE / P29-OPERATOR | 历史 `b2fb8b28` bootstrap 的 scope/commit/role replay 缺口已在 supporting baseline `6d46459e` 收口，不再沿用“尚未部署”的旧阻塞 | tenantless/exact replay、user+audit 显式 commit、typed refresh receipt 和真实 RLS 已有 supporting evidence；生产合成公司、管理员/成员邀请及 join 已完成 supporting 复验。2026-09-04 源码/33 backend preservation checks 与 2 AdminLayout checks 继续保持；当前 manifest 双遍未完成 | 保持 finding-level supporting Verified，不迁移任何 persona Journey PASS。新 GROVE 账号只待 browser action-time 权限确认，不是 bootstrap 未部署；final D 仍须完整 role/negative/双遍与 cleanup |
| RESPONSE-LEARNING-COMMIT-ORDER-001 | Fix Candidate | P1 | P05-J1 / P06-J2 / P09-MEMORY | Web Session 的 final model result 已产生、但 canonical terminal outcome 明确返回 `False`；event-loop drain 后同一未提交 run 仍产生 fast-reflection candidate 与下一轮 session-learning projection | 本地 candidate 已删除 Kernel/invoker 的低层 terminal hook，把 secret-redacted response payload 交给 canonical commit owner，并让 candidate/projection 按 committed receipt 幂等；独立 review 仍证实 commit→sidecar crash 无 durable pending/ack/recovery | 复用现有 durable outbox/recovery 范式补齐 postcommit crash recovery 与 stable ack；证明 crash replay 不漏、不重、不误封后续 turn，再做完整 cross-domain 回归与 production 验证 |
| SESSION-SUMMARY-COMMIT-ORDER-001 | Reproduced | P1 | P05-J1 / P06-J2 / P09-MEMORY | canonical terminal outcome 明确 hold 前，Kernel 已调用 production `persist_runtime_memory`；它在独立事务改写 `ChatSession.summary/last_message_at`，未提交 final 可经 Session recall 回流后续 Agent | `persist_memory` 属于低层 model loop dependency，没有 terminal commit receipt；正常、error、cancel、budget 与 checkpoint 均可提前写用户可见/可搜索 projection，现有 response-learning 测试曾以 no-op writer 掩盖此路径 | Kernel 只返回 summary 候选；canonical caller 在 committed outcome/transcript 后刷新，并绑定 terminal event/sequence watermark，证明 hold/abort 零 projection、旧 replay 不覆盖新 summary |
| OPERATOR-AUTHORITY-001 | Reproduced | P1 | P29-CADMIN / P29-OPER | 历史 production delegated `manage` 可replace-all并扩权，permission GET泄露主体，非法scope静默改写；旧合同还把管理员私有内容读取一并记为缺陷 | PDEC-013下管理员业务访问已获权，不再强制独立grant；非管理员generic manage仍不能扩权、看别人的私有内容或用inspector绕过自己/公开Agent边界。严格schema、真实actor审计、撤销/失效和secret投影继续有效 | 与ROLE-CONTRACT-ALIGNMENT-001同批对齐共享authority及正负回归，保留有效根因修复，不原样发布旧grant-only管理员门；新D前无production修复或新合同PASS |
| TOOL-ARTIFACT-SETTLEMENT-001 | Verified | P1 | P01-MAIN / PJ-02 / PJ-04 | `write_file` effect 已完成，但 canonical terminal `tool_call`/`tool_result` 与 ChatArtifact 在 `chat_artifacts_message_id_fkey` 处回滚；kernel 仍准备下一 provider round | `c37fefc5` 已原子提交 owner/artifact/V2/outbox 并在持久化失败时 hard-stop；`3482b57a` 对 exact unknown-effect invocation fail closed，唯一 operator acknowledgement 保持 unknown fact、禁止旧轮重放并释放 fresh-turn admission | normal/reload 与 supported recovery/no-replay 已 production PASS；保持 `Verified`，完成 clean P01-MAIN/PJ-02/PJ-04 双遍、authority-negative 与 cleanup 后才可 `Closed` |
| SESSION-RETRY-INPUT-001 | Verified | P1 | P01-MAIN / P02-STREAM | edit branch 的 canonical `human_input.accepted` 保存完整 retry prompt，但首个 `result_commit.prepared.bound_input_ids=[]`；provider 未调用工具并错误回复“这条消息只有「1」”，产品仍把 run/final 标成 `completed` | exact commit `2cee9f3e` 的 production retry Session `b3962147…` 已把完整输入绑定为唯一 `bound_input_id=1fd5cc5b…` 并进入 GLM/Work Ledger；随后失败属于独立的 tool-artifact settlement 与 provider 429，不回退本 finding | 保持 `Verified`；完整 P01/P02 双遍、recovery、authority-negative 和 cleanup 后才能 `Closed` |

#### IDENTITY-FIXTURE-BOOTSTRAP-001 复现与本地候选

- 已部署 `b2fb8b28` 的 code path 把 tenant、case-insensitive email user locator、row locks、User membership/quota mutation 和 `AuditLog` insert 放在同一 audited bypass transaction；但原 scanner 只收集 `select/insert/update/delete` 的内层参数，改变 predicate、lock、ORM attribute 或 `.add()` target 都不会改变 allowlist signature。failing-first security regression 在新 API 未实现前 collection RED，并用五个 AST variant 机械证明这些变化必须产生不同 fingerprint。
- source/auth proof：access JWT 保存签发时 role/tid，但 `get_current_user` 对 tenantless token 重新读取 canonical DB User、重新 pin tenant，`require_role` 使用 DB role；因此 `reauthentication_required=true` 与“必须重新登录”不成立。真实 PostgreSQL test 使用 assignment 前签发的 tenantless member token，在 assignment 后直接解析为同 tenant `org_admin`，无需新 token；已打开客户端只需刷新 profile。
- authority/resource proof：compatibility ID route 只做 tenantless membership bootstrap；同 tenant 同 role 返回 typed `already_assigned`，不同 role 必须走正式 tenant user management，跨 tenant 仍 409。目标 quota 无条件继承 target tenant；daily/month/total/reset 是 employee-level counters，assignment 原样保留，不再用它们猜 membership provenance。
- transaction proof：user mutation 与 AuditLog 同事务 `flush+commit` 后才构造 success receipt；commit failure 路由回归为 503 且 rollback，无先发 200。真实 PostgreSQL audit FK failure 同样 rollback membership；assignment 前签发的 tenantless token 在 committed assignment 后按 canonical DB row 立即取得新 authority。
- RLS/UI proof：AST fingerprint 同时覆盖 direct bypass scope 与 statically discoverable contextmanager consumer，predicate/lock/ORM write/add target 或 wrapper caller predicate 变化都会改变 hash。UI 在确认弹窗返回前锁定 intent；取消零请求、连续 Enter 一次请求、失败解锁；typed exact replay 显示 already-assigned 文案。
- latest local evidence：backend route/security/真实 PostgreSQL **30 passed**；RLS fingerprint 独立复跑 **9 passed**；frontend mounted **4 passed**；Ruff check/format 绿。finding 尚未 production Verified，tenantless synthetic admin 仍未被绑定或提权。

#### OPERATOR-AUTHORITY-001 当前源码复现

- live API root：`get_agent_permissions` 只要求普通 Agent access，却返回全体 grantee identity；`update_agent_permissions(data: dict)` 只要求 delegated `manage`，对非法 access 静默降级、先删除该 Agent 全部 permission rows，再 replace-all；`private/user + empty ids` 还会自动给当前操作者 `manage`。
- privacy root：`check_agent_access` 把同租户 org-admin 或 delegated grant 投影为同一个 `manage`；`authorize_session_action` 与 Session list/read paths 再把 `manage + 任意非空 reason` 当跨用户私有内容 authority。前端从 `access_level === manage` 推导管理/审阅能力，并多处自动发送 `Agent session administration`，不是操作者真实理由。
- 历史2026-09-04 decision：曾将私有内容读取统一要求独立 `operator.inspect`；PDEC-013已替代该管理员前置门。当前按上表新合同实施，普通employee不能因generic manage/旧operator grant扩权；严格schema、真实actor审计、缓存撤销和cleanup继续保护有效边界。

#### RESPONSE-LEARNING-COMMIT-ORDER-001 复现与本地候选

- live path：旧 `turn_orchestrator` 在 final response 分支 fire-and-forget `RESPONSE_COMPLETE`；`web_chat_run_orchestrator` 随后才尝试 canonical terminal transaction。RED 让 commit 返回 `False` 后仍实际落盘 fast-reflection candidate 与同 ID session-learning projection，独立复跑为 **1 failed**。
- 本地 candidate：Kernel 只返回 response payload；invoker 做 exact-secret redaction且不发任何 `RESPONSE_COMPLETE/SESSION_END/TURN_STOP`；Web canonical commit owner 生成 outcome/result/event/runtime refs 后才调度 `RESPONSE_COMPLETE`。consumer 缺 committed receipt 必须 hold；candidate ID 从 response idempotency key 稳定生成，candidate + projection 共用 `AgentAssetTransaction`，replay 不重复触发 Skill flywheel。
- sibling caller：business Task 只在 atomic Task/RuntimeTask finalizer 首次 transition 后发 terminal hook；Trigger/Delegation 不再在低层 invoker 提前终结。主 Codex 整合回归 **304 passed**，Ruff/format/diff check 绿；独立 reviewer 另跑 **52 passed**，其中真实 PostgreSQL terminal-outcome atomic/idempotent 用例 **1 passed**。
- Recovery 尚未闭环：Web、business Task、Trigger、Delegation 的 durable commit 与 best-effort hook/learning 之间仍有 crash window；同状态 replay 也不能用普通 bool 返回值证明“首次 transition”。因此 finding 只到 `Fix Candidate`，不写 `Verified/Closed`，更不升级 Journey。

#### SESSION-SUMMARY-COMMIT-ORDER-001 复现

- live path：Web 把真实 `memory_session_id` 交给 Kernel；Kernel 正常 final 返回前调用 production `persist_runtime_memory`，后者生成 summary 并用独立 transaction commit `ChatSession.summary/last_message_at`；Web 之后才尝试 canonical terminal outcome。`TerminalOutcomeIneligible`/reconciliation 不会回滚该 projection。
- abnormal path：`_persist_before_exit` 在 provider error、cancel、token/tool-round budget 与 mid-loop checkpoint 也调用同一 writer；这些平台错误/候选文本没有 assistant-final authority。
- Consumption 不是无害缓存：owner API 返回 summary，Session recall fallback 用它匹配/排序并通过 memory tool 回流 Agent；`last_message_at` 还改变会话排序。
- production-shaped RED 将原 no-op writer 改为 spy；当前精确顺序为 `['summary_projection', 'canonical_hold']`，期望只有 `['canonical_hold']`，定向结果 **1 failed**。这是新的 Evidence → Consumption 最早错误状态，当前记 `Reproduced`。

#### SESSION-AUTHORITY-PRESENTATION-001 复现、修复与生产验证

- production exact commit `3482b57a`、signed-in `platform_admin` 直接打开不属于当前 principal 的 Session URL，且未提交 operator view/reason。message 与 lineage API 正确返回 `Session not found`，DOM 没有跨用户 title/message/transcript/artifact；但前端渲染“会话 / 完成 / Read-only · User / 1 个步骤 / 运行错误”及 runtime/artifact/activity shell，server verdict 与 product presentation 不一致。
- immutable FAIL evidence：`evidence/3482b57a383d3c5bd33a5bcf813b87c6fab23339/P29-PADMIN-fault-denied-session-shell.md`。该文件只证明负向断点，不进入 NPTCR。
- source wiring：direct route 从 `listSessions('mine')` 找不到目标后构造 `is_pending_session_lookup=true` 的只读占位 Session；`selectSession()` 在 403/404 catch 仍构造 `runtime_action_failed/session_load_failed` timeline event，`AgentChatSection` 因 active Session 未清除而展示完整 workbench。
- failing-first mounted test 在 candidate 前精确失败：找不到 `role=alert`，DOM 仍含 `Read-only · User` shell。candidate 后覆盖 403、404、pending no-shell、successful resolution 与返回恢复；最终 frontend **154 files / 1148 tests passed**，i18n 双语各 3993 keys、production build 与 AgentDetail/vendor budgets 通过，Weekend/atomic architecture **24 passed**。
- candidate 只消费 exact `ApiError.status in {403,404}`，属于 server authority/machine contract，不扫描自然语言。它在解析中只显示 skeleton；authority terminal 时清空该 Session timeline/replay/event/runtime 状态并显示 denied/not-found；网络/5xx 继续走既有 durable retry，成功解析继续进入 verified read-only Session。
- production course correction：`d4ae15fd` 已消除假 shell，但从 denied route 返回共享 HR Agent 的 chat 会被产品自动选中另一不可访问 Session；`57823bcf` 排除了 stale ref/Effect 重入后，production hard reload 证明根因是目标 Agent 的默认 chat auto-selection。最终 `bbf6d234` 复用既有数字员工列表作为安全恢复边界，按钮文案为“返回数字员工”并导航 `/agents`，没有新 abstraction 或后端改动。
- exact `bbf6d2340afe593b44f740fabfa178d126b5beca` 已 push；Railway backend `4ad99e93-d3be-48c9-be8d-0107dff44f82`、backend-api `8aa5ccbc-fe9d-4da2-bb39-f16497de044f`、frontend `638da152-1ef6-444c-bcd8-4dd00fa0296d` 均 `SUCCESS`，backend health `status=ok`，frontend HTTP 200。
- signed-in `platform_admin` 对同一负向 URL 的 production DOM 只显示“找不到此会话 / 此会话不存在，或当前账号无法访问 / 返回数字员工”，没有 `Read-only · User`、完成、运行错误、会话交付物或跨用户正文。点击后 URL 精确为 `/agents`，无 stale denied/not-found alert；随后 hard navigation 到合法 MAPLE Session 仍显示 marker `P01-MAIN-PASS1-3482B-MAPLE-581`、完成终局、3/3 todos、一个 artifact、0 running/0 waiting，且无 authority alert。
- immutable production verification：`evidence/bbf6d2340afe593b44f740fabfa178d126b5beca/SESSION-AUTHORITY-PRESENTATION-001-production-verification.md`。该证据只把 finding 推进到 `Verified`；P29 正向 platform health/compliance、双遍、role-change recovery、四角色 matrix 与 cleanup 未完成，NPTCR 仍为 `0/96`。

#### RUNTIME-GUARD-PRESENTATION-001 复现、修复与生产验证

- deployed `bbf6d234`、signed-in `platform_admin` 只读打开 `/enterprise/runtime-budgets`：section heading/badge 为“被保护的任务 0”，同一 section 却有 5 条 `active` run、5 个“暂停”按钮，且每条原因均为“系统保护机制已介入”。没有点击暂停或保存。
- live wiring：`WorkspaceRuntimeBudgetsSection` 计算的 `protectedRuns=[]`，却用 `protectedRuns.length > 0 ? protectedRuns : runs.slice(0, 5)` 渲染普通 recent runs；backend `_user_reason('active', None)` 未有 explicit branch，落入 intervention 默认值。错误同时存在于 canonical API presentation 与唯一 control-plane consumer，不是纯翻译问题。
- 最小共享修复：backend explicit active reason 为“运行正在正常进行”；frontend 在无 protected run 时把同一 fallback 列表/计数/说明标为“最近运行”，继续保留 active “暂停”控制；一旦存在 protected run，仍沿原路径优先显示 protected section。无 schema、迁移、依赖或持久配置改动。
- production-shaped RED：backend helper 精确得到旧 intervention 字符串；frontend static render 精确得到 protected heading + active row + intervention reason。GREEN：focused backend 8 / frontend 6，相邻 backend 87 / frontend 142；完整 backend **8439 passed, 2 skipped, 1 warning**、frontend **154 files / 1149 tests**，i18n 3995/3995、Ruff/format、production build/budgets、24 architecture tests 与 manifest validator 全绿。
- exact `6a6695e88d915a0e37b44e64dcdfe5bdd90a9454` 已 push；Railway backend `cdef3ce1-85e6-4662-a5aa-a6fb9793a21b`、backend-api `2261b169-3c8a-4c3e-a42b-7a1239b2b8e2`、frontend `feb46b17-e017-457a-8c09-b94065730ce1` 均 `SUCCESS`，backend health `status=ok`，frontend HTTP 200。
- production hard navigation 后页面显示“最近运行 5 / 最近的运行活动；正在运行的任务可在此暂停。”；5 条 active row 全部显示“运行正在正常进行 / 等待当前运行完成”，5 个暂停按钮仍在，旧“系统保护机制已介入”和“被保护的任务”均不在 DOM。证据：`evidence/bbf6d2340afe593b44f740fabfa178d126b5beca/P29-PADMIN-fault-active-runtime-guard-presentation.md` 与 `evidence/6a6695e88d915a0e37b44e64dcdfe5bdd90a9454/RUNTIME-GUARD-PRESENTATION-001-production-verification.md`。
- finding 推进为 `Verified`；当前没有 production protected run 用于正向 protected-state screenshot。后续 `LLM-PROBE-AUDIT-001` 已关闭 provider health audit 断点，但 P29 其余 API/compliance evidence、fault/reload、pass-2、四角色 matrix 尚未完成，所以 P29 不写 PASS，NPTCR 保持 `0/96`。

#### LLM-PROBE-AUDIT-001 复现、修复与生产验证

- deployed `6a6695e8` 的 signed-in `/enterprise/llm` health Test 会真实调用外部 provider，但 `test_llm_model()` 没有 audit writer；`/enterprise/audit` 无对应事件，前端也只消费 agent-bound legacy audit log。pre-fix bounded verdict 为 MiniMax success `7623ms`、DeepSeek 一次 `HTTP 402 Insufficient Balance`、GLM success `7575ms`；没有重试 DeepSeek、充值、换 credential 或修改模型配置。
- 最小共享修复在 provider effect 前写并 commit `llm_model.test_started`；provider success/failure 后写 `llm_model.test_completed`。两者共用生成的 probe/request ID，只持久化 provider、model、max_tokens、phase、success、latency 或 exception type。started audit 不可用时 HTTP 503 且禁止 provider call；effect 后 terminal audit persistence 失败则返回 `retryable=false` typed result，保留 started evidence 并禁止自动重试。
- `/enterprise/audit` 复用 selected-tenant server authority；frontend 并行读取 canonical security audit 与 legacy operational audit，合并排序后展示 action/event/severity/resource。没有 schema、migration、dependency、feature flag 或持久配置。
- RED：正确 Python 3.12 venv 下 backend 5 failures、frontend 2 failures；GREEN：focused backend 6、selected-tenant API file 22、frontend adjacent 34。full gates：backend **8443 passed, 2 skipped, 1 warning**；frontend **154 files / 1149 tests**；i18n 3995/3995、9 node tests、Ruff/format、production build/budgets、24 architecture tests、manifest validate 与 diff check 全绿。
- exact `cc6e726218bd491120f942edfa91e51d2d167ff4` 已 push；首次部署因手工错误扩展 short SHA 且脚本未 fail-fast，三个空上传 deployment `446bb56e…` / `771d44b3…` / `7f139625…` 均立即 `FAILED`，未替换运行实例。恢复后以 `git rev-parse HEAD`、`set -euo pipefail` 和 archive 内容检查重新上传；backend `f619e4a9…`、backend-api `7edd592d…`、frontend `beb9cd36…` 均 `SUCCESS` 并绑定 exact full SHA，health/HTTP 通过。
- post-fix 只点击 GLM Test 一次；probe `a0f1be98-27bd-4d69-9bde-247b57c6b16c` 在 `05:21:32` started、`05:21:36` completed，`zhipu/glm-5.3`、`max_tokens=16`、`success=true`、`latency_ms=3411`。audit hard reload 后 started/completed 各一、同 probe ID 恰出现两次、无 raw API key、无第二次 provider call。
- immutable evidence：`evidence/cc6e726218bd491120f942edfa91e51d2d167ff4/LLM-PROBE-AUDIT-001-production-verification.md`。finding 推进为 `Verified`；P29 四角色/双遍/fault/negative 与 P33 三模型 frozen compatibility tasks 仍未完成，NPTCR 保持 `0/96`。

#### AUDIT-DEFAULT-DISCLOSURE-001 复现、修复与生产验证

- exact deployed `cc6e7262` 的 signed-in `/enterprise/audit` 默认合并 400 条记录；DOM 量化为 `session_id=110`、`job_id=94`、`issues=94`、`reason=41`、`agent_name=77`、raw `Insufficient Balance=90`。无需 operator reason 或展开即可读到用户 recovery note、Session/job identity 与 raw provider payload。
- live API trace 证明 legacy list 返回完整 `details/user/ip`，canonical list 返回完整 `details/ip/user-agent/hash/execution identity`；raw details 还参与 admin search 并进入 CSV。export/chain 直接使用 home tenant，未复用 platform-admin selected tenant resolution/pinning。
- 最小共享修复保留 action、actor/resource、hash identity 与明确 model/runtime summary fields；server response、CSV 与 frontend consumer 均用 exact key allowlist，raw details 不再参与 admin search。canonical DB row/hash input 不改写；list/export/chain 共用 `resolve_and_pin_tenant_scope()`。没有 schema、migration、dependency、feature flag 或生产数据变更。
- RED：backend 4、frontend 1；GREEN：backend adjacent 30、frontend module 3。full gates：backend **8448 passed, 2 skipped, 1 warning**；frontend **154 files / 1149 tests**；i18n 3995/3995、9 node tests、Ruff/format、production build/budgets、35 architecture tests、manifest validate 与 diff check 全绿。
- exact `b23e94210e7e9523bafc3b591b35db8fc2762224` 已 push；backend `03d0919e…`、backend-api `b0bb7ca3…`、frontend `0dd299d8…` 均 `SUCCESS` 并绑定 full SHA；health `ok`、RLS strict、runtime bus no error、frontend HTTP 200。
- production hard reload 后仍有 400 条记录；GLM probe ID 恰两次且 provider/model/success 可读，六类 raw/default disclosure counts 全部为 0。跨用户 Session hard navigation 仍收敛到 truthful not-found，无 workbench/artifact/body。
- immutable evidence：`evidence/b23e94210e7e9523bafc3b591b35db8fc2762224/AUDIT-DEFAULT-DISCLOSURE-001-production-verification.md`。finding 为 `Verified`；单一 platform-admin 身份不能替代 P29 四角色/双遍/完整 fault-negative evidence，NPTCR 保持 `0/96`。

#### PLATFORM-ADMIN-BUSINESS-BODY-001 复现、修复与生产验证

- exact deployed `b23e9421` 的 signed-in `/enterprise/info` 默认 DOM 直接显示公司介绍正文标记 `AI agents for teams`、legacy-file surface 与 broadcast controls；live source trace 同时证明 raw `/enterprise/info` 和 `company_intro*` system-setting route 允许 platform admin 读取或改写业务正文。
- 最小共享修复在 backend 现有 route 用 authenticated role + exact setting prefix 拦截 platform admin，org admin 语义不变；frontend 只对 org admin 请求、挂载和保存 business content，platform admin 保留 tenant identity/timezone/presentation 与 truthful role-boundary callout。没有 schema、migration、依赖、feature flag 或生产数据变更。
- `170c30e8` 首次生产部署后主体 section 已消失，但页面说明仍宣称 company profile/legacy export/broadcast 能力；该残余在同轮 hard reload 被捕获，`8f6a7263` 以一行产品文案和 mounted regression 收敛，而非把残余留成文档债。
- RED backend 4 / frontend 2；GREEN target backend 16、frontend 3，adjacent backend 52、frontend 37。full gates：backend **8453 passed, 2 skipped, 1 warning**；frontend **155 files / 1151 tests**；i18n 3997/3997、9 node tests、build/budgets、31 permission/RLS/RC architecture tests、manifest、Ruff/format 与 diff check 全绿。
- exact `8f6a726375452042cf1252977394c647dd2aba80` 已 push；backend `35e6d6e5…`、backend-api `86615c7d…`、frontend `cfa5f254…` 均 `SUCCESS` 并绑定 full SHA；health `ok`、RLS strict、runtime bus no error、frontend HTTP 200。
- production `/enterprise/info` hard reload 后新说明与 role-boundary 各一，company intro/pre-fix body/legacy export/broadcast/runtime error 均为 0，tenant name/timezone 保留；audit 400-summary 与 denied Session route 同时保持既有安全结果。
- immutable evidence：`evidence/8f6a726375452042cf1252977394c647dd2aba80/PLATFORM-ADMIN-BUSINESS-BODY-001-production-verification.md`。没有读取浏览器 token/localStorage 来制造直接 production API 403 回执；FastAPI route-entry 与 exact deployment 证明 backend wiring，单一 platform-admin 身份仍不能替代 P29 四角色/双遍/完整 fault-negative evidence。finding 为 `Verified`，NPTCR 保持 `0/96`。

#### PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001 复现、修复与生产验证

- exact deployed `8f6a7263` 的 platform admin dashboard 展示全量 company workspace；九个 direct URL 挂载业务 DOM，company lifecycle API 为 200，Agent authority 还把 platform role 自动升级为 blanket manage。
- `24f012ba` 在 shared workspace registry/route guard、backend route-entry 与 Agent permission helper 修复同一 authority root；platform admin 只保留八个 platform/config/health tabs，Agent scope 只保留 ownership/exact-user。没有 schema、migration、dependency、feature flag 或 production data change。
- D1 production hard reload 发现 sidebar 仍显示“公司后台”；`bf94b76a` 复用既有 `nav.superAdmin` 以一行实现和 mounted regression 收敛残余。
- exact `bf94b76a1706510daf2d11c4e98fd5051f23f28f` 已 push；backend `07059ce5…`、backend-api `c70ff972…`、frontend `308e7789…` 均 `SUCCESS` 且 message 绑定 D2，health `ok` / RLS strict / runtime bus no error，frontend HTTP 200。
- production dashboard nav 精确 8 项、card 7 项、只显示后台页面指标，无 User/员工/审批指标或 Plaza；九个 company direct URL 全部回到 dashboard，0 row/email/UUID business DOM。authenticated status-only 矩阵的 stats/approval/org/invitation/legacy/User/external/Guard/Knowledge/HR/Plaza API 全部 403，未读取 header、storage 或 response body。
- `/agents` hard reload 仍为 200，EventPilot owner/manage surface 可见，system HR 为 403；info 与 audit 允许路径保持 200 且无 company body/raw audit disclosure。full gates：backend **8484 passed, 2 skipped, 1 warning**、真实 PG **13 passed**、platform-admin contract **423 passed**、frontend **156 files / 1161 tests**、build/budgets、Weekend **18 passed**、manifest、Ruff 与 diff check 全绿。
- immutable evidence：`evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001-production-verification.md`。finding 为 `Verified`；P29 四角色/双遍/role recovery 未完成，NPTCR 保持 `0/96`。

#### SYSTEM-SETTING-SECRET-DISCLOSURE-001 复现、修复与生产验证

- exact deployed `8f6a7263` 的 Feishu setting 对 signed-in platform admin 为 200；generic GET/PUT 原样返回 stored value，unknown key 还可落入 global setting。reproduction 未读取任何 production secret 或 response body。
- exact `bf94b76a` 用 role/key allowlist 在 selected-tenant/DB 访问前 fail closed；Feishu GET/PUT 统一移除 `app_secret`，只返回 `app_secret_configured`，合法 stored value/update effect 保留。
- production status-only probe 对 `/api/enterprise/system-settings/feishu_org_sync` 为 403；探针只复用既有 request header 并读取 status，未读取/输出 token、storage、header 或 body，未发送 PUT、触发 sync、修改或轮换 credential。
- synthetic route regressions 覆盖 platform/org role-key negative、missing/GET/PUT response projection 与 stored-value preservation；同一 full gate 和 exact D2 三服务部署证据通过。
- immutable evidence：`evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/SYSTEM-SETTING-SECRET-DISCLOSURE-001-production-verification.md`。finding 为 `Verified`；当前缺少 signed-in org-admin production 200 projection screenshot，该缺口留在 P29 四角色矩阵，不写 Journey PASS，NPTCR 保持 `0/96`。

#### TOOL-ARTIFACT-SETTLEMENT-001 复现证据

- production application `2cee9f3ec09c7191ed4eda3c70a7c01206341b89`；Session `b3962147-07cd-4223-8f23-f00193d7735c` / RuntimeTask `76a32f8e-f5d8-5a63-b02a-e591598321e9`。
- canonical sequence `304` 为 `tool_call.started`，`305` 为 `effect_started`；受治理 `write_file` 已把 `workspace/WEEKEND-RC-P01-MAIN-PASS-1.md` 从 size `1914` / SHA prefix `52313b…` 改为 size `1508` / SHA prefix `ffdb3f…`，但没有 matching `tool_call.completed` / `tool_result.completed`。
- production log `2026-08-30T15:15:26Z` 记录 PostgreSQL `ForeignKeyViolationError`：`ChatArtifact.message_id=32e6d45a-6bfd-5f9c-920b-14f7db5c98eb` 没有对应 `ChatMessage`；事务回滚 canonical settlement，但 kernel 在 sequence `308` 仍准备 provider round six。
- 后续 sequence `309/310` 是独立的 Zhipu HTTP 429 / code `1302` typed terminal；rate limit 不是 FK 根因，也不能把缺失 receipt 变成 retry-safe。
- signed-in reload 如实显示 `失败`、0 running、0 waiting、0 delivered artifacts、Work Ledger 1 completed / 2 open；没有自动 replay，但用户仍无法从预期 artifact surface 消费已写文件。
- immutable FAIL evidence：`evidence/2cee9f3ec09c7191ed4eda3c70a7c01206341b89/P01-MAIN-fault-rate-limit-artifact-settlement.md`；该文件不进入 NPTCR。

#### TOOL-ARTIFACT-SETTLEMENT-001 修复候选与验证

- 无 production DDL。canonical terminal path 使用 invocation-derived deterministic message ID，并在同一事务内先创建 tenant/agent/principal/session-bound compatibility `ChatMessage`，再创建 `ChatArtifact`、`tool_call.completed`、`tool_result.completed` 与 outbox；无 artifact 时删除 provisional owner，settled replay 不重新读取已删除 workspace source。
- terminal settlement 异常会在独立恢复事务把 invocation 标成 `needs_reconciliation`，保留原 evidence 且不生成语义 tool result；typed `ToolLifecyclePersistenceError` 同时终止串行、并行和 pre-effect callback 旁路，RuntimeTask 不生成 assistant prose、禁止 automatic retry 并保留 exact file-change facts。
- frontend Session V2 serializer/reducer 投影 `message_id`；canonical tool pair 以该 ID 取代 compatibility row，同时消费 canonical artifact parts，避免重复卡片或附件丢失。compatibility anchor 不持久化 governed args 或 raw result，只持久化 provider-visible projection。
- production-shaped RED：真实 PostgreSQL 精确复现 FK failure 且 invocation 停在 `effect_started`；并行 pre-effect fence 测试证明旧分支会吞 typed failure 并继续 provider。GREEN：真实 PG 覆盖 FK/anchor/artifact/V2/outbox/idempotent replay/quarantine/legacy ordering，kernel 串并行均 hard-stop。
- final local gates：核心交叉 **330 passed**；完整 backend **8428 passed, 2 skipped, 1 warning**；frontend **1143 passed**、production build 和 AgentDetail/vendor bundle budgets 通过；Ruff、format、`git diff --check`、manifest `valid=true` / denominator `96` / hash `d320edce…` 均通过。
- application commit `c37fefc56b92e658bfb64a3e79d685249a2a3add` 已 push；Railway backend `62e4ef56-7e6b-456e-a505-fea90fd286a0`、backend-api `307f0df7-6ae0-4c57-817e-f9ca07fd59fc`、frontend `db6b605d-7b8b-40ea-8da8-247259db29f8` 均 `SUCCESS` 且 message 绑定该 exact commit。公共 backend health `status=ok` / `runtime_control_bus.last_error=null`，frontend HTTP 200；这些只关闭部署原子。
- 新部署后的 signed-in reload 保持旧故障 run 为 `失败`、0 running、0 waiting、0 artifacts，没有自动重放不确定 write effect。
- fresh normal revalidation：owner action-time 确认后只发送一次 `D3-SETTLEMENT-C37-8K4P`；Session `0731ec15-c662-4552-9500-3f68f1094f11` / RuntimeTask `c124e51f-c09e-5b0d-9265-38b48ae0db27` 在 GLM-5.3 下 `completed`。canonical invocation 恰为 `write_file` 一次、`read_file` 一次，两个 span 均 `status=ok`，两个 invocation 均 `effect_committed`、无 recovery owner。
- write canonical order 为 sequence `121 started → 122 effect_started → 123 tool_call.completed → 124 tool_result.completed`，下一 provider round 到 `127 result_commit.prepared` 才开始；read 为 `167 → 168 → 169 → 170`，下一 round 到 `173 prepared` 才开始。write terminal pair 共用 message ID `07afe8cd-ff96-5c03-b0f1-e54ca9c12462`；对应 ChatArtifact `be17c252-8a97-4782-ae3e-17e05d2f3519`、ChatMessage owner、目标 path/snapshot 均恰一行；terminal outbox 均 `published`、attempts 1、no error。invocation/event/run reconciliation 计数全为 0。
- artifact snapshot 为 77 B，三行正文无尾随换行，content SHA-256 `2c3f309736338d6185614a50e56875de7fc1092cd239c765b7df1661f7ec07e6` 与期望字节完全相等；canonical read tool-result event `24dabf4f…` 的 529 B provider-visible wrapper `contains_expected=true`，完整包含同一 77 B 字节。normal UI 显示精确 final、一个文件、一个 session artifact、0 running/0 waiting；hard reload 后仍为同一 Session/run/tool pair/artifact，无自动 replay；普通用户从「打开」入口成功消费保存快照预览。
- 机械取证使用 Railway backend 内 `asyncpg` readonly transaction + tenant `set_config`，仅显式 tenant/session SELECT 并 rollback；无 credential 输出、DDL、DB 写或 RLS 绕过。探针窗口 backend/backend-api 对 `ForeignKeyViolationError`、`ToolLifecyclePersistenceError`、`needs_reconciliation`、`tool_lifecycle_persistence_failed`、`chat_artifacts_message_id_fkey` 过滤均为 0。immutable bounded evidence：`evidence/c37fefc56b92e658bfb64a3e79d685249a2a3add/TOOL-ARTIFACT-SETTLEMENT-001-normal-revalidation.md`。
- 上述 `c37fefc5` 证据在当时只关闭 normal/reload/Consumption 子路径；它本身不证明 supported recovery，也不把 P01-MAIN/PJ-02/PJ-04 写成 PASS。后续 `3482b57a` 证据单独关闭该恢复门。
- `3482b57a` recovery candidate 统一使用 `SessionToolInvocation.result_event_id IS NULL + effect_state in {effect_started, needs_reconciliation} + recovery_owner non-null + terminal RuntimeTask` 的 exact predicate；fresh turn、branch、central run admission、抢先 admitted-input worker recovery、Session workbench 与管理员队列共用该事实源。operator acknowledgement 只追加 `tool_call.reconciled` / `recovery_action.reconciled`、清除 operational hold 并停止旧 NR task；invocation 仍保留 unknown state，绝不制造成功/失败 tool result 或重放旧 provider round。
- failing-first 回归实现前 4 项失败；实现后后端定向 **310 passed**、完整 backend **8438 passed, 2 skipped, 1 warning**，frontend **1145 passed**、i18n、production build/bundle budget 全绿；Ruff/format、diff check、18 条 Weekend/atomic tests 与 manifest validator 均通过。结构门曾因 AgentChatSection 2439>2400 失败，未放宽阈值，提取独立 recovery/feedback surface 后降至 2392 行并通过。
- application commit `3482b57a383d3c5bd33a5bcf813b87c6fab23339` 已 push；Railway backend `7c196980-34c6-4846-bf25-0397b7b55c0e`、backend-api `8e7545b8-9b6c-4b32-a77d-48883191728a`、frontend `6f6bd18c-1681-4049-ac20-6660a3f84fc3` 均 `SUCCESS` 且 message 绑定该 exact commit。
- production read-only precheck：旧 D2 Session hard reload 后显示 unknown-effect alert，generic `重试本轮` 消失，composer/发送 disabled，0 running/0 waiting；管理员队列把 run `76a32f8e…` 提升到首项，只显示必填 evidence note 与 disabled acknowledgement，没有 generic resolve/archive/retry。该 pre-action checkpoint 见 `evidence/3482b57a383d3c5bd33a5bcf813b87c6fab23339/TOOL-ARTIFACT-SETTLEMENT-001-recovery-admission-precheck.md`。
- production supported recovery：只对 `76a32f8e…` 填入已核验 workspace 文件事实并点击 acknowledgement 一次。目标 invocation `1dcbdf47…` 仍为 `needs_reconciliation`、`result_event_id=null`，`recovery_owner` 清空，receipt 指向 sequence `312 tool_call.reconciled`；sequence `313 recovery_action.reconciled` 与前者 outbox 均 `published` / attempts 1。旧 RuntimeTask 保持 `failed`，没有制造 `tool_result`、artifact 或第二个旧 run。
- fresh-turn/no-replay proof：同一 Session 新 input `ad602cdc…` 只绑定独立 run `f8cdd9ac…` 的唯一 round，0 tool invocation，sequence `375` 逐字为 `D4_RECOVERY_OK`，sequence `385/386` 正常终局。Session hard reload 为 prompt/final 各一、0 blocker/running/waiting/Stop；Workspace 原文件与 marker/两个验收字段各一；管理员目标 row 0、error 0。完整证据见同目录 `TOOL-ARTIFACT-SETTLEMENT-001-recovery-verification.md`。
- 因 normal path、failure hold、operator evidence action、canonical no-result reconciliation、no-replay 与 fresh-turn release 均已在 exact deployed code 上成立，本 finding 推进为 `Verified`。authority-negative、cleanup 与完整 P01-MAIN/PJ-02/PJ-04 双遍仍 open，故 Journey 不写 PASS，NPTCR 保持 `0/96`。

#### SESSION-RETRY-INPUT-001 复现证据

- production application commit `d0c9fffd1ca4995ddea6d367e04e206e973560d5`；失败源 Session `d1a2c63f-7082-424d-a9f3-a3330398e371` / run `ff9536bd-39fa-5bf3-bd02-f07aa6fb0e81`，edit retry branch Session `ef9d6498-f4dc-49c1-a566-6446e220f0ef` / run `03419d5f-6166-479d-ad02-d929759c57df`。
- 源 run 在 3-step plan、3 todos、受治理 `write_file` + `read_file` 后于 final 前收到 typed `provider_error`；目标文件已正确生成。点击产品唯一一次“重试本轮”后，branch run 无 tool calls、Work Ledger 为空、无新 artifact effect，却返回“看起来这条消息只有「1」”并被 terminalized 为 `completed`。
- branch canonical transcript seq `1` 是完整 1,300+ 字符 P01-MAIN prompt，含 marker `P01-MAIN-P1-CEDAR-734`、固定表格与受治理写读要求；branch lineage 为 `mode=edit`、source/root `d1a2c63f…`、anchor `b0004973…`、`copied_event_ids=[]`。
- signed-in operator workbench 的 `hive.session_semantic_history_receipt.v1` 合法返回 `status=empty`、`event_count=1`、`message_count=0`，说明 fresh edit branch 没有应继承的 prefix；但 model request seal 的 `bound_input_ids=[]`，且 runtime summary `used_tools=[]`。因此错误位于 current-run input admission/binding，不是 `SESSION-CONTEXT-001` 的跨轮 history 复发。
- current checkout wiring proof：正常 create/start Session routes 调 `submit_live_human_input()`，后者创建 Session V2 command/input、运行 Hook admission 并由 `session_input_dispatch` 用确定性 run ID 启动 `append_user_message=False` 的 RuntimeTask；branch route 却直接调用 `start_web_chat_run(..., append_user_message=True)`。kernel provider loop 只从 canonical history 与 `round_input_bind()` 获取 user messages，不消费 `RuntimeTask.prompt` 作为普通当前输入，所以这一旁路会发送无当前输入的 provider request。
- Attempt 1 与 retry 均保持 `FAIL`；文件没有被 retry 重写，故未发生重复 effect，但“无 effect + 错 final + completed”本身是 P1 假成功。NPTCR 仍为 `0/96`。

#### SESSION-RETRY-INPUT-001 修复候选与本地验证

- live-entry candidate 只改变 branch API 的 run admission：`edit`、`insert_before`、`insert_after`、`reply`、`side_question` 统一调用 `submit_live_human_input(requested_kind="start_turn")`；input ID 和 idempotency key 由已创建 branch Session 与 mode 确定性派生。若 admission 不产生 run，branch receipt 保存 typed input/admission/dispatch 状态；不伪造 completed。
- `regenerate` 不代表一条新 HumanInput，继续以 `append_user_message=false` 启动并消费 branch 已复制的 canonical user prefix；专项测试阻止重复 checkpoint。
- production-shaped RED：API entry 测试命中 legacy `start_web_chat_run()` 旁路；真实 PostgreSQL live API 测试在 branch Session 下找不到 `SessionTurnInput`。GREEN：同一测试证明 1,300+ 字符 Unicode prompt 字节忠实成为唯一 round-one user message，`SessionModelResult.bound_input_ids_json` 精确包含该 input ID。
- 定向 branch/history/accepted-prompt-first **43 passed**；加 Session V2 input control 与 Weekend/atomic gates 的 cross-domain **139 passed**；完整 backend **8419 passed, 2 skipped, 1 warning**。Ruff check 全仓通过，三条变更代码/测试的 Ruff format check 通过，manifest `valid=true` / denominator `96` / hash `d320edce…`，`git diff --check` 通过。全仓 format check 报告 43 个不属于本次 diff 的既有未格式化文件，按 scope preservation 未修改。
- immutable production failure evidence：`evidence/d0c9fffd1ca4995ddea6d367e04e206e973560d5/P01-MAIN-fault-provider-overload-retry-input-loss.md`。
- production application `2cee9f3ec09c7191ed4eda3c70a7c01206341b89` 的 supported retry 建立 Session `b3962147-07cd-4223-8f23-f00193d7735c` / run `76a32f8e-f5d8-5a63-b02a-e591598321e9`；round one 的 `bound_input_ids` 精确为非空 `1fd5cc5b-8378-5629-8cdc-98fd8250f27f`，GLM 消费完整 prompt 并创建 3 个 todos。随后暴露的 FK settlement P1 与 provider 429 是新的独立断点，因此本 finding 推进为 `Verified`，但 Journey 仍为 `FAIL`、NPTCR 仍为 `0/96`。

### 已验证、关闭门待补

| ID | 状态 | Severity | Journey | 最早错误状态 | 当前根因边界 | 下一动作 |
|---|---|---:|---|---|---|---|
| SESSION-CONTEXT-001 | Verified | P1 | P01-MAIN / P02-STREAM | 同一 Session 第二轮 provider request 未获得上一轮 user/assistant 语义历史，模型明确称“这是本会话我收到的第一条消息” | exact commit `d0c9fffd` 已把 tenant/agent/session-bound canonical transcript、committed model seal、settled tool results 与 anchored legacy rows 接入 live runtime；无固定消息窗口、无不可用 silent fallback，并覆盖 rewind/branch/current-run ownership | 保持 `Verified`，完成完整 P01-MAIN/P02-STREAM signed-in 双遍及 production fault/recovery、authority-negative 后才可按关闭合同推进 `Closed`；无需再改同一根因代码 |

#### SESSION-CONTEXT-001 复现证据

- production application：`eb61d468221aa22a4f22c1d96353baadef3b51e6`；实验 Session：`59257e7a-960b-459a-9652-2ff39be117ee`。
- 第一轮 run `2fa2f887-b76e-556c-99c8-3a814c37f27b` 正确生成 No-Go 判断及可观察恢复证据；第二轮 run `58b222f2-b52b-5cb1-b5a1-f657ced4222a` 在相同 Session 审计上一轮时否认存在上一轮。
- `GET .../transcript?limit=1000&schema_version=2` 返回 sequence `1..635`，包含两组 `human_input.accepted`、`assistant_text.snapshot`、`run.completed`；第一轮 prompt 和完整 assistant snapshot 均可从 canonical payload 读取。
- `GET .../messages` 返回 10 行，全部为 `system`（model route、memory degradation、context window、provider ledger），user/assistant 为 0。
- current checkout wiring：`web_chat_runtime._load_runtime_context()` 查询 `ChatMessage`；`web_chat_run_orchestrator` 把该 history 转为 `state.conversation`；Session V2 当前输入另由 `bind_round_inputs()` 注入。因此第二轮只有本轮 input，旧轮语义历史未进入 provider request。
- 这不是“Memory degraded”本身：Memory 事件明确说明可继续，且同一 Session 原始对话历史属于 Session lifecycle，不应依赖 Personal/Agent Memory 检索。
- 本次证据只建立 P1 与最早断点，不是最终根因修复设计，也不构成 P01/P02 PASS。

#### SESSION-CONTEXT-001 修复与生产复验证据

- application commit：`d0c9fffd1ca4995ddea6d367e04e206e973560d5`；Railway backend `ce0bdbf4-c8b6-4cd3-bbe2-77e74a75ca2e`、backend-api `ef4f7c81-b8cb-44d8-bbd7-37499e1765fb`、frontend `f6932ba1-9f7e-4b61-8b38-54ae709ba278` 均为 `SUCCESS` 且 message 绑定该 exact commit。
- fresh production Session：`3ce68041-ccc4-4d4e-b729-ec9ace46d222`。P01 probe run `71cffdb6-ef6b-53fa-9a63-ea57ac98349f` 的用户输入含唯一 marker `HIVE-CANONICAL-Q7M4-83NP`，assistant 只输出 `ACK-FIRST`，因此没有把 marker 复制到 assistant history。
- P02 probe run `40c3e678-0ca9-59f8-8abd-e65ef64a4cf9` 的当前输入不含 marker，却正确输出 `HIVE-CANONICAL-Q7M4-83NP NO_TOOL`；这证明真实 provider path 消费了上一轮用户语义及“未要求工具”的上下文。
- 通过受支持的 signed-in operator workbench 读取 P02 `hive.session_semantic_history_receipt.v1`：`status=complete`、`truth_source=chat_transcript_events+session_model_results`、`message_count=2`、`user_checkpoints=1`、`committed_provider_messages=1`、`settled_tool_results=0`、`mechanical_message_limit_applied=false`、`held_items=[]`，并在 `excluded_current_run_input_ids` 中排除 P02 当前输入。P01 receipt 为 typed `empty`，符合 fresh Session 预期。
- 以上关闭了已复现根因的 production 语义回归，但没有覆盖 P01-MAIN 的开放任务/deliverable，也没有覆盖 P02-STREAM 的 Markdown streaming、terminal/reload/duplicate/flicker，更没有完成 production fault/recovery 和 authority-negative；因此 finding 仅为 `Verified`，两条 Journey 均不写 PASS，NPTCR 保持 `0/96`。

### 尚待 fresh reproduction

| ID | 状态 | Severity | Journey | 观察/假设 | 下一证明动作 |
|---|---|---:|---|---|---|
| UI-CMD-001 | Observed | P2 candidate | PJ-03 | `/skill` 与 `/agent` 可能返回目标 subview，但 Agent extensions/selector 未消费目标，仍停在默认 catalog | signed-in UI 分别输入命令，记录 URL、selected tab、目标对象和 reload |
| UI-CMD-002 | Observed | P2 candidate | PJ-03 | `/workflow` 可能只切换 tab，没有打开指定 draft/preview | signed-in fresh draft 逐字段复现，追踪 `ui_action → route → consumer` |
| UI-CMD-003 | Fix Candidate | P2 | P03-CMD07 / P03-CMD08 / P03-CMD10 | exact `bf94b76a` 的 fresh production Session 中，`/context` 短暂排队后消失；`/usage`、`/permissions` 只显示 generic completed + raw Session ID，hard reload 后三者全部消失 | fix commit `1b4be5d2` 已补 typed `ui_action`、novice-readable panel、URL reload 与 RuntimeTask/InvocationSpan usage 去重，并已包含在当前 production `b2fb8b28`；production verification pending，仍缺 signed-in employee normal/reload、权限负向和同提交双遍，故 Finding 未 `Verified`、三条 Journey 未计分；见 [production reproduction](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/UI-CMD-003-production-reproduction.md) |
| KNOWLEDGE-UI-001 | Observed | P1/P2 candidate | PJ-09/PJ-10/PJ-11 | Agent Knowledge 消费 `entries + pages`，可能把 Agent Memory、Personal KB、Company KB 混成一个不诚实状态 | 从 employee Agent Detail 逐层核对来源、owner、authority 和空/拒绝/不可用状态 |

除 `UI-CMD-003` 已 fresh reproduction 外，其余仍为候选，未复现前不得修改代码或宣称根因。

## Setup 与 external readiness（不伪装成产品 finding）

2026-09-04 文档 correction review 的 `RC-PROFILE-FREEZE-001` 属于本地验收测试缺口，不是已发生的生产越权：CC 与 Codex 各自在副本给 `profiles.employee_session.allowed_effects` 加入未授权外发效果，原 journey/scoring 指纹和 manifest validator 仍通过；实际 profiles 与 HEAD 完全相同。zCode 只把 profiles 纳入现有冻结子集指纹并加反例证明；CC 后续 review 与 Codex 独立复算均确认 HEAD/current/固定常量同为 `b9fc6e3b…e80bd`，全文件 manifest hash 未变，eval/architecture 387 项通过。此本地测试缺口已核对，不代表 M0/Journey 完成；不改 manifest 授权、不新增 runtime gate。原 CC receipt `4ea7301c00a94593be5210560978de27`，作者 `437e6112807444a6a084ed63986eaee2`，首轮代码 review `a5da5e54f1d4491ca4ed7e2205581d22`。

2026-09-04 M0 本地修正尚未整体收口：`J4-ATTEMPT-OWNERSHIP-001` 的直接 runtime/state 根替换误删、初始化失败误删与 Hive 未 claim 写入，和 `J4-DENIED-EVIDENCE-READ-001` 的 pre-claim 拒绝后 foreign 读取，已由 CC correction review `c8ed1de1d38d4e83b9e0a0bad064839f` 与 Codex 后续实测核对。两方同一候选 `bbc15ec3…`，Codex eval/architecture **392 passed / 24.09s**；仅使用 test-owned synthetic data，不涉及真实私密内容。

但边界仍有遗漏：CC → Codex 复现 artifacts/scoring 兄弟目录软链接写穿、post-claim 祖先替换后 foreign workspace 被 hash；Codex 另证只替换 workspace 真实目录也仍报 completed/boundary_ok/cleanup_verified=true，替换内容进入 scoring input。此项使用 mocked provider process 与真实 adapter/filesystem，不是 semantic score。目录身份验证与未知证据需要贯穿现有消费路径，不能把路径、内容 hash 或外层目录身份当成 workspace 所有权。cleanup 文档须准确承认 trusted owner root 与路径竞态的上限，不扩建通用文件系统隔离框架。

`BRIDGE-PAIRING-RETIREMENT-RACE-001`：Codex原真实PostgreSQL探针 **1 failed / 7.25s，SQLSTATE40P01**，隐式FK锁使identity→pairing与exchange互锁。zCode修正tenant retirement和单用户offboarding后，CC合并review`2859cb988de7498b89bb2d9b9a43932a`独立复证两类缺锁RED与正确候选14real-PG三次GREEN，21path起止及Codexhash复查一致。但CC/Codex发现测试屏障仅证明发出SQL，错误预锁谓词仍 **4 passed / 14.28s**；还独立复现fresh unbound pairing在offboarding提交后获approved。后者exchange403且0active连接，没有证明可用幽灵连接，但approval终态和回归固定力仍需修正。新zCode`76295`/`1ad1281ae07b441d909d3197248968fd`负责共享binding/identity顺序、真实锁证据与typed refusal，覆盖ensure-default先创建真实Agent的入口，避免孤儿副作用；不扩大普通tenant delete或新增全局锁框架。当前并非M0接受或production修复。

同次独立核对还补充两项本地门：`_workspace_receipt`的None/替换identity被升级为可信，Codex **2 failed / 0.25s**，交同一zCode收口到既有receipt/score消费；RLS allowlist **1 failed / 48.64s**，准确指出WS ticket扩大查询shape与exchange live identity新scope未登记，仅允许更新对应真实delta。共享immutable scorer字节已有hash验证，不要求每个envelope另造目录；已存在的mismatch用新owned output目录恢复，不覆盖或删除未知文件。

17:33 UTC取回`76295`时，wrapper success/end_turn的正文实际以provider `write EPIPE`结束。Codex核对仅local_bridge_service.py落入partial live-check/rollback改动，其他20path未变，四项残余无完整验证；不记修复完成。已同范围续做`79241`/`d64b6bc0804b48e387dcab953192afe6`，保留已有工作，未重装/换模型/操作凭据。approval原子后置校验方案仍须证明隐式FK锁、真实Agent入口及零孤儿效果，不把源码推论当真实PG证据。

2026-09-05入口切片Kimi`24743`已交付，`SIDEBAR-COLLAPSED-SETTINGS-001`与`WORKSPACE-BACK-TO-APP-001`已完成CC→Codex本地复查；窄屏Session的managed内容与resize角候选也经Kimi`56914`、CC`f6dda663…`及Codex核对成立。Codex继续补出的列表首屏约18px/原生拖动受cap封死及danger ConfirmModal聚焦删除红例，已由Kimi`89115`修正、CC`39badbad…`首审并经Codex读码/图与串行3+25 E2E、tsc复证接受。Linux图仍是明确x64 CI门。Kimi`56914`曾在新`/tmp`副本和Docker执行`npm ci`，违反任务no-install流程；repo依赖未改，独立host验证仍支持代码真值，Linux自报只作辅助。所有入口/UI候选均未进入final D/production验收；HR/完整角色UI与Knowledge另行接入，未从完整目标移除。PDEC-013最后CC源码review`26485`/`8b3c86e439f14a6e9e2d8b6e951f2089`已结束，Codex独立核对后方案已收敛，结论见角色域与前端域。真实actor、原owner、精确审批/投递与HumanBrowser/AgentRuntime边界必须到消费端；不得将旧privacy-deny测试作为新管理员功能的完成标准，代码重大节点尚未通过。

| ID | 状态 | 历史事实 | 允许的当前动作 |
|---|---|---|---|
| BLOCKER-J4-RUNTIME-001 | IMPLEMENTATION_QUEUED | frozen P08-J4 要求 Hive/FreeCode/Hermes 同 task/workspace/model/resource envelope；current manual runner 只有官方 Claude Code/Hermes targets，FreeCode 未构建且 Hive live runner 已退役 | 保留历史空报告、不造分；从仓库与 lockfile 构建 FreeCode/Hive adapter，缺预编译 CLI 不再是 blocker |
| BLOCKER-MODEL-001 | EXTERNAL_UNAVAILABLE | MiniMax 与 GLM bounded production probe 成功；DeepSeek exact binding 已确认，但唯一 live probe 返回 `HTTP 402 Insufficient Balance` | 不充值、不换真实 credential、不重复调用；验证 typed unavailable、audit、角色呈现、恢复指导和无关模型/工具保留，external readiness 单列 |
| BLOCKER-BRIDGE-001 | RECOVERY_QUEUED | Hive Connect daemon running，但 `hive-connect status` fresh 返回 `401 Invalid bridge token`，UI linked `0` / offline | 通过支持的 lab re-login/pair/session-token/binding 路径恢复并验证 revoke/reconnect；不读取真实组织 secret |

外部 readiness 在最终交付中单列；setup/adapter 工作由 Codex 完成，任一路径都不以尝试次数永久阻断整个 Goal。

## 严重度

| 级别 | 定义 |
|---|---|
| P0 | 越权、跨租户泄漏、数据破坏、不可逆错误、全局不可用 |
| P1 | 核心旅程阻断、永久非终态、假成功、证据丢失、不可恢复 |
| P2 | 外部测试者可见且显著破坏理解或信任 |
| P3 | 不阻断任务的小型一致性或美观问题；不能自动延期 |

## Finding 关闭合同

每个 finding 必须链接：冻结 journey、最早错误状态、live-entry wiring proof、production-shaped failing regression、最小共享根因修复、focused/cross-domain/full/真 PG/build gates、exact commit、三服务部署、signed-in pass 1/2、fault/recovery、authority negative 和 rollback。

禁止只隐藏 UI、字符串猜语义、放宽断言、用 fake pin 孤儿路径、在失败 provider 上盲重试，或把一个能力的证据外推给另一个能力。
