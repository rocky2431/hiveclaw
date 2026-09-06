---
document_id: weekend-rc-2026-09-06-p01-hr-preview-production-verification
owner: Codex
status: active
authority: production-hr-confirmation-and-provisioning-evidence-not-nptcr-pass
last_reviewed: 2026-09-06
verification_status: canonical-blueprint-confirmed-and-provisioned
journey_id: P13-HR / P01-MAIN precondition
environment: production
source_commit: 3c92053466b26e872c21a7c7e0b50d37ae6342ea
deployed_commit: 3c92053466b26e872c21a7c7e0b50d37ae6342ea
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: WRC-P01-EMPLOYEE-AGENT-R1-20260906
started_at: 2026-09-06T20:16:00+08:00
ended_at: 2026-09-06T20:19:00+08:00
result: PROVISIONED
fault_recovery_result: NOT_RUN
negative_authority_result: NOT_RUN
cleanup_result: NOT_RUN
---

# P01 HR blueprint and provisioning production verification

## Input and execution

- Chrome was authenticated as synthetic employee `WRC M1 Employee CEDAR R2` in tenant `0430e023-de03-4e8c-a3dc-b2a63e751427`.
- Fresh HR Session `3b0b01e8-3819-455d-bade-58a042323845` accepted the same bounded brief once with marker `WRC-P01-EMPLOYEE-AGENT-R1-20260906`.
- The Session ran on GLM-5.3, completed six visible steps, and invoked `load_skill`. It explicitly stated that all required identity, work-contract, governance, and capability gates were present and that it would preview rather than create.
- The Session completed in approximately 3 minutes 18 seconds with zero running/waiting work and rendered an `AGENT 蓝图预览` card.

## Canonical draft

- Authenticated read-only API readback returned blueprint ID `048a0ec3-e19a-449b-95cb-3e3f59317722`, version `1`, hash `bp_133714e0424802b944e4cf77`, status `awaiting_confirmation`, and name `WRC P01 CEDAR Worker R1`.
- `created_agent_id` and `confirmed_at` were null. The draft expires at `2026-09-13T12:19:00.317633+00:00`.
- `ready_now` contains only builtin tools plus nine default skills and the standard workspace/memory/heartbeat/self-evolution scaffolding. `will_install`, `warnings`, and `manual_steps` are empty.
- The rendered identity and first-work contract require understanding an open operations task, publishing the plan before execution, maintaining Work Ledger, writing only Markdown under `workspace/` through governed file tools, reading it back, and reporting the verified path/result.
- The rendered boundaries prohibit external Skills/MCP/connectors, external messages, web access, credential access, other Agents, workflow/trigger/automation, company-level data, direct unmanaged disk access, and fabricated success.
- The card identifies company-member availability and ordinary risk. Its information-source expansion attributes the substantive fields to the authenticated user's confirmed brief, with only the greeting draft attributed to general role knowledge.

## Confirmation gate

- The exact card presented `确认并创建`, `要求修改`, and `拒绝` actions and remained `等待你确认` until the owner explicitly authorized all necessary Weekend RC actions.
- Codex clicked `确认并创建` once for blueprint `048a0ec3-e19a-449b-95cb-3e3f59317722` version `1` / hash `bp_133714e0424802b944e4cf77`; no revision or alternate draft was consumed.
- Authenticated production readback showed `confirmed_at=2026-09-06T12:32:24Z`, provisioning task `ce3dad21…`, attempt `1`, and all seven provisioning steps completed without a failure.
- Provisioning created Agent `4e5261a6-c182-5248-9ca1-669f9419d44f`, `WRC P01 CEDAR Worker R1`. The Agent is owned/created/sponsored by CEDAR R2, uses `zhipu/glm-5.3`, standard execution, request-approval defaults, self-only employee visibility, zero plugin/MCP/external snapshots, and the nine default skills shown by the UI.
- The Agent detail page independently exposed the same identity, role, model/provider and bounded capability surface. This is supported-path HR/provisioning evidence, not a P01-MAIN pass.

## Not proven

- P01-MAIN pass 2, terminal-boundary recovery, fault recovery, authority-negative behavior, cleanup, or any Journey PASS. P01-MAIN pass 1 was attempted separately and is recorded as a breakpoint because its canonical terminal boundary did not settle.
