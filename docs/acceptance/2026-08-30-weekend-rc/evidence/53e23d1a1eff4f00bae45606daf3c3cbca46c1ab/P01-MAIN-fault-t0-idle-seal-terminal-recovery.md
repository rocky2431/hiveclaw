---
document_id: weekend-rc-p01-main-fault-t0-idle-seal-terminal-recovery
owner: Codex
status: active
authority: production-failure-evidence
last_reviewed: 2026-09-07
verification_status: reproduced-idle-seal-root-cause-final-candidate-local-review-accepted
journey_id: P01-MAIN
pass: fault-recovery
environment: Railway production
source_commit: 53e23d1a1eff4f00bae45606daf3c3cbca46c1ab
deployed_commit: 53e23d1a1eff4f00bae45606daf3c3cbca46c1ab
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
deployment_ids: backend=73543680-3332-473f-9990-6cf3ff111fbf; backend-api=b62c7f07-a399-4534-a918-98cbb8770d44; frontend=b4a7defb-a4d4-415e-a1ee-a3124451861b
persona_principal: synthetic CEDAR R2 member; exact tenant 0430e023-de03-4e8c-a3dc-b2a63e751427
data_version: p01-main-pass1-cedar-7k9m-existing-outbox-attempt-9
started_at: 2026-09-06T14:31:29Z
ended_at: 2026-09-06T14:55:00Z
result: FAIL
fault_recovery_result: FAIL
negative_authority_result: BLOCKED_PRECONDITION
cleanup_result: BLOCKED_PRECONDITION
supersedes: none
finding_id: T0-IDLE-SEAL-TERMINAL-RECOVERY-001
---

# P01 T0 idle-seal terminal recovery failure

## Input

After exact `53e23d1a` passed Harness run `34037372686` and all three services were deployed from one clean archive, the existing P01 terminal outbox `7b200f1c-4d3f-5240-b344-e8706138aed7` was read in `dead_letter`/attempt 8. Its 1,033 transcript events were all `projected`, and the Session had no summary projection state. The supported operator endpoint was invoked once with a reason only; `summary_disposition` was omitted.

## Authority

The request used the recoverable synthetic R3 `org_admin` through the production same-origin API and the selected fixture tenant. The secret was read only from macOS Keychain and was not printed or persisted. Owner authorization covers supported-path synthetic recovery. No role, tenant, RLS, credential, or database field was changed directly.

## Execution

- The first local HTTP attempt failed TLS verification before sending a request and had no production effect.
- The one real `POST` returned HTTP 200 at `2026-09-06T14:31:29Z`. Audit row `df70806a-4ddc-4c25-a1d3-0c21d113bc65` records exact outbox, previous attempt 8, and no summary disposition.
- The worker claimed the row once. It returned to `dead_letter` at attempt 9 with `last_error=WebTerminalBoundaryPending`; `delivered_at` and receipt remained null. No second redrive was sent.

## Evidence

- Exact CI: backend 9,082 passed / 3 skipped / 2 warnings plus prompt, reward, and internal gates; frontend unit/contract and 77 Playwright passed; atomic 15/15 passed.
- Exact production source identity: `ca6c8e0b449ca76b7ee5d38a514436c563ff5d23bb9914289600d5c7a8465d5f`, file count 1,056, independently matched by backend and backend-api. Public backend health and frontend HTTP 200 passed.
- Tenant-scoped read-only database evidence bound the outbox terminal to database transcript sequence 1,033 and T0 event `evt_ee53c2c3f6dd42e2ad4783a5f1a64eb6` in segment `seg-20260906T123523Z-dfd42344`.
- The supported employee files API read the T0 index and events without mutation: there is exactly one segment; it is sealed at `2026-09-06T13:38:56Z` by `SESSION_IDLE` with reason `idle_timeout`; its boundary is sequence 1,034 and `active_segment_id` is null.
- The same read proved the exact stored production shape: the segment and events at sequences 1,032 (`turn.completed`), 1,033 (`run_outcome.terminal_committed`), and 1,034 (idle boundary) contain the exact RuntimeTask ID but no top-level or metadata `turn_id`, alias, or idempotency entry.

## Recovery

The ordered transcript drain worked and left no projection frontier. Recovery still failed because idle handling sealed the exact T0 segment after the terminal event but before the required terminal boundary could seal it. Current `seal_t0_session_segment` replays a stable boundary if that boundary already exists, otherwise it only seals the active open segment and returns null when no active segment exists.

The first zCode candidate (`a4c7e9a…`) was blocked by CC `1fc1e01e…` and independently by Codex because it returned the idle event as canonical identity, performed an unlocked index alias write that could lose concurrent segment state, and failed the exact format gate. The `7367958d…` correction removed those writes and normalized the canonical receipt, but still required a stored turn and therefore did not match the live no-turn segment.

The production-shape correction `0c2054b7…` was frozen at four SHA-256 values `5dcd5e2b…`, `0965d644…`, `48e60625…`, and `895e8c17…`. CC `51c3cf03…` independently obtained 65 candidate passes and five old-behavior failures, then blocked its candidate-only alias reads with no writer and processor tests that replaced the required T2 projector with a no-op. Codex independently reproduced 65/65 and the exact five red failures, confirmed the dead alias reads, and read the production stable T2 job through the supported employee files API: `t2job-fe4358a24b4a4c2a` is `held`/`held`, which is already an accepted durable required-T2 state. A local no-turn production-shape probe using the real required projector passed without changing T0 or the held manifest. A failed manifest remains a truthful required-projection failure and must not be weakened into acceptance. Two existing real-PG tests independently passed and prove canonical outbox authority remains single at the application boundary.

zCode `ff19df81…` then removed the dead alias machinery and misleading arbitrary-second-identity assertion and added real-projector held/failed coverage. The four frozen final-candidate hashes are `6782fae6…`, `04c4a44a…`, `3067d292…`, and unchanged direct twin `895e8c17…`. CC `bdb19fa5…` independently accepted the byte-frozen candidate after tracing Web/direct required projection, running focused 67, outbox/reconciliation 59, T0/T2/control-bus 278, three real-PG authority checks, Ruff/format, and eleven read-only fail-closed probes. Its two broader local failures were reproduced unchanged on clean HEAD and attributed to the installed `lark_oapi` warning/module shape, so exact CI remains authoritative.

Codex independently read every changed line and all production callers. The candidate focused set passed 67/67; the same seven new tests against clean `53e23d1a` all failed at the intended old behavior; outbox/session/reconciliation passed 59/59; hooks/T0/T2/control-bus/transcript passed 278/278 from the required backend working directory; three real-PG canonical outbox tests and all 17 RLS bypass fingerprint tests passed; Ruff, format, and diff-check passed. The accepted design is stateless: it recognizes only the latest sealed segment when a UUID boundary, stable idempotency key, exact RuntimeTask, and turn are all supplied; it returns the caller-proven canonical boundary while retaining the actual idle boundary event and sequence, writes no index/event/manifest bytes, rejects mismatched stored turns and failed T2, and leaves unique canonical ownership with the existing transactional outbox. This is accepted local candidate evidence, not commit, CI, deployment, recovery, or Journey proof.

A terminal recovery must reuse only the unambiguous already-sealed segment bound to the canonical terminal run/task, return a truthful canonical receipt idempotently without rewriting T0 history, and never create or seal an unrelated successor.

No further redrive is allowed until that code path is fixed, independently reviewed, passed exact CI, and deployed.

## Consumption and acceptance

The employee-facing task, final, artifact preview, and hard reload remain visible, but canonical terminal settlement is incomplete. RuntimeTask `9332f6e7-012f-576b-a6e1-70725a7415c3` remains completed with null `completion_outbox_settled_at`. This fault recovery is `FAIL`; P01 remains a `Breakpoint`, pass 2 has not started, and NPTCR remains 0/96.

## Cleanup and not proven

The Session, Agent, accounts, invitation-derived membership, audit, outbox, and artifact remain registered synthetic assets for final cleanup. This evidence does not prove the pending code fix, attempt 10 recovery, fresh-run prevention, authority-negative behavior, rollback, or Journey completion.
