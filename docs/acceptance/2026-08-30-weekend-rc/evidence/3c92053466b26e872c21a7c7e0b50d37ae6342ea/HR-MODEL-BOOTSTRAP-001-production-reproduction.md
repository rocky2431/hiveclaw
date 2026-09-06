---
document_id: weekend-rc-2026-09-06-hr-model-bootstrap-production-reproduction
owner: Codex
status: active
authority: immutable-production-failure-evidence-not-nptcr-pass
last_reviewed: 2026-09-06
verification_status: reproduced-credential-backed-fixture-gap
journey_id: P13-HR / P01-MAIN precondition
environment: production
source_commit: 3c92053466b26e872c21a7c7e0b50d37ae6342ea
deployed_commit: 3c92053466b26e872c21a7c7e0b50d37ae6342ea
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: WRC-P01-EMPLOYEE-AGENT-R1-20260906
started_at: 2026-09-06T19:49:00+08:00
ended_at: 2026-09-06T19:54:03+08:00
result: BREAKPOINT
fault_recovery_result: NOT_RUN
negative_authority_result: NOT_RUN
cleanup_result: NOT_RUN
---

# HR model bootstrap production reproduction

## Input and authority

- Chrome was already authenticated as synthetic employee `WRC M1 Employee CEDAR R2`; Settings showed `成员`.
- An independent login response had already bound this principal server-side to tenant `0430e023-de03-4e8c-a3dc-b2a63e751427` with `role=member` and `needs_setup=false`.
- Home showed zero visible digital employees. The user-facing `创建你的第一个数字员工` action led to `/agents/new`, whose only creation action is `使用 HR Agent 引导创建`.
- That action created fresh HR Session `cae41732-3a3b-41b9-9ff2-477d5befcc9c`. Codex submitted exactly one creation brief with marker `WRC-P01-EMPLOYEE-AGENT-R1-20260906`.
- The brief requested a synthetic operations assistant able to plan, maintain Work Ledger, and use governed workspace file tools. It prohibited external Skills, MCP, connectors, messages, web, credentials, other Agents, workflow, trigger, automation, and company-wide data access. It explicitly requested preview first and no direct creation.

## Earliest wrong state

- The accepted HR turn terminalized immediately as `失败`.
- The only user-facing error was `当前 Agent 尚未配置模型。请在“权限与设置”中选择模型，或联系管理员配置后再重试。`
- The retry contract was `不可重试`; Codex did not retry the run.
- No model/provider call, tool invocation, blueprint preview, confirmation, provisioning, or digital employee effect occurred.

## Root-boundary evidence

- In the concurrently authenticated platform-admin browser, Codex selected exact fixture company `WEEKEND-RC-ROLE-FIXTURE-1B4BE5D2` through the supported company selector.
- `/enterprise/llm` displayed `暂无内容`; opening `添加模型` showed a required API Key field and disabled Test/Save actions until configuration is supplied.
- `/enterprise/hr` displayed the System HR Agent, but its AI-model selector contained only selected option `—`.
- Current deployed source matches this boundary: `LLMModelCreate.api_key` is required; tenant LLM models are tenant-scoped; HR Agent stores a tenant model ID. No supported cross-tenant credential/model-copy path was found.
- This evidence therefore proves a credential-backed fixture gap, not yet a product-code defect. Whether all newly created companies must receive a managed model without BYOK is a separate product decision.

## Acceptance and recovery

- P01-MAIN pass 1 did not start: the employee had no usable Agent, and the sole Agent-creation path could not run without a configured model.
- This record does not use `BLOCKED_PRECONDITION` as a Journey verdict: missing synthetic fixture/model configuration remains setup work under the frozen contract.
- Recovery requires the owner to enter a real provider API key directly in the already-open production UI, test and save it. Codex must not read, receive, log, or copy that credential.
- After save, Codex can bind the model to HR Agent and start a new HR Session. The non-retryable failed Session remains immutable evidence and a final-cleanup target.

## Configuration recovery follow-up

- The owner entered the provider credential directly in the production UI; Codex did not read, receive, log, or copy it.
- `/enterprise/llm` subsequently showed `GLM-5.3`, `zhipu / glm-5.3`, enabled and default. `/enterprise/hr` showed `glm-5.3 (zhipu)` selected; after navigating away and back, the same binding was read back.
- This closes the synthetic fixture's missing-model configuration gap. It does not change the terminal result of Session `cae41732-3a3b-41b9-9ff2-477d5befcc9c` or establish any Journey PASS.
- A separate fresh Session reached the provider lane and failed on provider balance/credit; that result is recorded independently in `HR-MODEL-PROVIDER-BILLING-001-production-reproduction.md`.

## Not proven

- HR blueprint preview/confirmation/provisioning, created Agent identity/model/tool surface, employee P01 pass 1, hard reload, pass 2, fault recovery, authority-negative behavior, cleanup, or any Journey PASS.
