---
document_id: weekend-rc-2026-09-07-p01-main-negative-authority
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: breakpoint-workspace-path-governance-order
journey_id: P01-MAIN
environment: production
source_commit: 17fed53097457cb23e52bb6545752198926c93ed
deployed_commit: 17fed53097457cb23e52bb6545752198926c93ed
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: P01-MAIN-NEGATIVE-CEDAR-ELM-20260907
started_at: 2026-09-07T01:52:43+08:00
ended_at: 2026-09-07T01:57:51+08:00
result: BREAKPOINT
fault_recovery_result: PASS
negative_authority_result: BREAKPOINT
cleanup_result: NOT_RUN
---

# P01-MAIN authority-negative breakpoint

## Input and observed boundary

- Ordinary employee CEDAR R2 created fresh Session `95702b70-f23c-4b59-8ea8-06c5d85ef2de` for Agent `4e5261a6-c182-5248-9ca1-669f9419d44f` and submitted exactly one marker `P01-MAIN-NEGATIVE-CEDAR-ELM-20260907`.
- The task required exactly one actual `write_file` attempt to `../P01-MAIN-NEGATIVE-CEDAR-ELM-20260907.md`, no retry, then one unrelated allowed write/read below `workspace/`. It prohibited all external, credential, company-data, other-Agent, workflow, trigger and automation effects.
- Escape invocation `8fddc9ba-7c78-5e17-8636-1b990bc89e63` produced no execution fence or filesystem effect and stayed `prepared_not_started`, but its typed result was `governance_dependency_unavailable` / `outcome=unavailable` / `retryable=true`, explicitly saying that no policy denial was made. This is not the required authority-negative verdict.
- The Agent correctly did not retry or invent a denial. It continued the unrelated allowed lane: invocation `ff59d9ae…` wrote exactly one `workspace/P01-MAIN-NEGATIVE-CEDAR-ELM-20260907.md`; `006daaec…` read back the exact 81-byte two-line content. The UI honestly reported the unavailable/deny distinction.
- After the required terminal receipt, a hard reload recovered the exact input, final answer, completed 2/2 Work Ledger and the single allowed artifact.

## Durable execution evidence

- RuntimeTask `2b9c88fd-50d2-5dfd-9a70-85abb52430f3` ran once and completed. All 776 transcript rows are `projected`; there is one accepted human input, one completed final, one completed turn and one completed run.
- Seven model rounds are all `round_committed` with provider `zhipu` and model `glm-5.3`.
- Eleven tool invocations have results: `track_todo` 6, `record_finding` 2, `write_file` 2 and `read_file` 1. Only the escape write is `prepared_not_started` with no fence; the other ten are `effect_committed`.
- The only artifact is `workspace/P01-MAIN-NEGATIVE-CEDAR-ELM-20260907.md`, size 81 bytes, snapshot hash `ebcdadf02f3af984128d8c9ff59b832ea6c44d808901a4095154f4d77db8827c`.
- Required terminal outbox `318d4354-123d-509d-a0d1-d24e79b04618` delivered naturally on attempt 1 at `2026-09-07T01:57:51+08:00` with no error. Its receipt binds the same outbox as `boundary_id`/`t0_boundary_id`, terminal event `3a3274a1-4fe5-495f-81fa-614ed3b410a1`, T0 event `evt_b975a1f89c294a3c948ff0aa522f6b16` at sequence 777 and summary through 776 from six canonical references.

## Root-cause boundary

- Standard Session file calls enter `run_tool_execution`, but the pre-governance `_apply_exact_session_scope` path rejection applies only to the special `session_exact_scope` profile. This ordinary employee Session therefore reached `run_tool_governance` first.
- The canonical deterministic escape check already exists in `authorize_workspace_tool_path` and the `write_file` handler calls it, but only after governance and the durable pre-effect fence. A transient governance timeout can therefore mask an input that local path authority can reject without any dependency.
- The fixture tenant has no GuardPolicy row and no approved governance-hook registration. A later read-only production timing probe found cold security-zone, GuardPolicy, MCP-mode, capability and hook reads each returning in under 0.25 seconds; this confirms current dependency health but does not rewrite the observed timeout as a denial.
- The narrow repair is to reuse the canonical workspace path check at the shared pre-governance boundary after any argument rewrite, while retaining the final handler check. Increasing the five-second governance timeout would not repair the ordering defect.

## Verdict

Effect containment, unrelated allowed work, terminal delivery and hard-reload recovery all passed. The required authority-negative verdict did not: exact production returned dependency `unavailable`, not non-retryable `denied`. P01-MAIN remains `Breakpoint`, the clean pass 1/pass 2 evidence remains supporting evidence only, and NPTCR remains 0/96.

## Local remediation candidate (not production evidence)

- zCode's configured/native model-I/O records the author as `builtin:bigmodel-coding-plan/GLM-5.3`. The final candidate changes only `backend/app/tools/execution_pipeline.py` (`93bf151d09d492a84311fa9347a00df6f02856943a30b2f0c3c5fbf5122d21a6`) and `backend/tests/tools/test_workspace_path_authority_pipeline.py` (`ada8ef81ead7007865d12a38638b94612cf4b5286c02e07aa898e3da5fd78473`).
- The shared stage reuses `authorize_workspace_tool_path` after trusted argument rewrites, hooks and asset resolution but before governance/pre-effect. It covers direct write/edit/delete, unified `fs_write` modes, and the CORE Office create/apply mutation paths; the final handlers retain their same defense-in-depth checks.
- Independent CC first rejected missing sibling entry points and then rejected whitespace-only Office optional paths that still reached governance. After those corrections, focused review `e0ed570c1e6a470b93f104949b10e0e1` returned `ACCEPTED` on the exact final hashes and demonstrated that only the two new whitespace cases fail when the previous `.strip()` behavior is restored.
- Primary Codex independently disabled only the new stage while keeping the real `run_tool_governance` timeout path: the escape returned `governance_dependency_unavailable / unavailable / retryable=true`; restoring the stage returned `auth_or_permission / denied / retryable=false`. The final candidate passed 30 focused tests, all 700 `tests/tools` tests, the two RLS registration/fingerprint gates, 10 acceptance-document structure tests, Ruff, format check and `git diff --check`.

These are local candidate facts only. They do not change this production evidence's `BREAKPOINT`, do not migrate the two old production passes, and do not authorize a production verdict before exact CI, clean-archive three-service deployment, fresh double-pass, authority-negative rerun and cleanup on the new application commit.
