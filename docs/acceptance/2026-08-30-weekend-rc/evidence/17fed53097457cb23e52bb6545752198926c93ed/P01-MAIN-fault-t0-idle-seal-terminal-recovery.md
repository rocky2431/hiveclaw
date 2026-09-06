---
document_id: weekend-rc-2026-09-07-p01-idle-seal-recovery
owner: Codex
status: active
authority: production-fault-recovery-evidence
last_reviewed: 2026-09-07
verification_status: exact-deployed-recovery-pass
journey_id: P01-MAIN
pass: fault-recovery
environment: Railway production
source_commit: 17fed53097457cb23e52bb6545752198926c93ed
deployed_commit: 17fed53097457cb23e52bb6545752198926c93ed
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
deployment_ids: backend=8ae57a4e-d70a-4585-b273-4659ef3c7f71; backend-api=b1d4ee8c-1760-49a4-9635-3daeec29f101; frontend=f90753bb-d77b-4c16-be48-14d0691878b1
persona_principal: synthetic GROVE R3 org_admin; exact tenant 0430e023-de03-4e8c-a3dc-b2a63e751427
data_version: p01-main-pass1-cedar-7k9m-existing-outbox-attempt-10
started_at: 2026-09-06T17:03:00Z
ended_at: 2026-09-06T17:12:57Z
result: PASS
fault_recovery_result: PASS
negative_authority_result: NOT_RUN
cleanup_result: NOT_RUN
supersedes: evidence/53e23d1a1eff4f00bae45606daf3c3cbca46c1ab/P01-MAIN-fault-t0-idle-seal-terminal-recovery.md
finding_id: T0-IDLE-SEAL-TERMINAL-RECOVERY-001
---

# P01 idle-sealed terminal recovery production pass

## Release identity

- Exact commit `17fed53097457cb23e52bb6545752198926c93ed` passed Harness `34046037891`: backend 9,089 passed / 3 skipped / 2 warnings plus prompt, reward and internal gates; frontend unit/contract and 77 Playwright passed; atomic 15/15 passed.
- All three services were deployed from one clean Git archive. Backend, backend-api and archive independently matched source identity `f904b76e9050fd686d41d51197ce2d80cb4954766e9b84eb8875a931a30cf51e` with 1,056 files. Backend health, strict `app_rls`, four daemons and frontend HTTP 200 passed.

## Controlled recovery

- Before mutation, the supported employee files API read T2 job `t2job-fe4358a24b4a4c2a` as `held`/`held`; tenant-scoped read-only PostgreSQL found outbox `7b200f1c-4d3f-5240-b344-e8706138aed7` at `dead_letter`, attempt 9, with no receipt.
- The authorized GROVE R3 company administrator sent exactly one supported redrive request with a reason and no summary disposition. It returned HTTP 200. The audit count for the exact outbox increased once, from one to two; the new audit recorded previous attempt 9.
- The worker delivered attempt 10 at `2026-09-06T17:07:42.990624Z` with no error. No retry request was sent.

## Idempotency and durable receipt

- The receipt binds `boundary_id` and `t0_boundary_id` to the exact outbox UUID and terminal event `315a6938-9143-4ec5-ae64-6740bfb28785`.
- It truthfully reuses the original idle boundary T0 event `evt_9ee3b354909e47fabc5a76c3290947ff` at sequence 1,034 and summary through transcript sequence 1,033. The T0 index remained one sealed segment, `active_segment_id=null`, `next_sequence=1035`, with the original `idle_timeout` event and no alias or adoption write.
- Input, final, 13 tool invocations/results, five committed model rounds and the single artifact remained unchanged. The transcript stayed exactly 1,033 projected rows. T2 remained `held`/`held`.

## Corrected settlement interpretation

`RuntimeTask.completion_outbox_settled_at` remains null because `web_chat_turn` is intentionally outside `COMPLETION_OUTBOX_TASK_TYPES`; it is not this task type's settlement signal. The authoritative web-turn settlement is the existing terminal execution fence plus the required terminal boundary outbox receipt. Expired claimant fields are retained fenced history, not an active claim.

This closes the exact idle-seal recovery finding in production. It does not by itself close P01-MAIN, migrate NPTCR, prove a second clean pass, cover authority-negative probes, or authorize cleanup.
