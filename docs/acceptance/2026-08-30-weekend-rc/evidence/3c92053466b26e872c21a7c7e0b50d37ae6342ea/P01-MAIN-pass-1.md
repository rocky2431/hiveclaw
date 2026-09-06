---
document_id: weekend-rc-2026-09-06-p01-main-pass-1
owner: Codex
status: active
authority: production-journey-evidence-not-pass
last_reviewed: 2026-09-06
verification_status: breakpoint-terminal-projection-dead-letter
journey_id: P01-MAIN
environment: production
source_commit: 3c92053466b26e872c21a7c7e0b50d37ae6342ea
deployed_commit: 3c92053466b26e872c21a7c7e0b50d37ae6342ea
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: P01-MAIN-PASS1-CEDAR-7K9M-20260906
started_at: 2026-09-06T20:35:23+08:00
ended_at: 2026-09-06T20:39:00+08:00
result: BREAKPOINT
fault_recovery_result: NOT_RUN
negative_authority_result: NOT_RUN
cleanup_result: NOT_RUN
---

# P01-MAIN production pass 1 breakpoint

## Input and visible product result

- Chrome was authenticated as ordinary employee `WRC M1 Employee CEDAR R2`; the selected Agent was `WRC P01 CEDAR Worker R1` (`4e5261a6-c182-5248-9ca1-669f9419d44f`).
- A fresh Session `3351fede-216a-44d5-9dfc-21cce7313356` accepted exactly one prompt with marker `P01-MAIN-PASS1-CEDAR-7K9M-20260906`.
- The bounded open task required a public plan, a Work Ledger, one governed workspace file, write-then-read verification, exact launch/absence/handoff/rollback facts, and no external or cross-Agent effects.
- The UI completed on GLM-5.3 in about 3 minutes 45 seconds. It showed a plan before effects, Work Ledger `4/4`, one final answer, one artifact, and zero running/waiting work.
- A hard reload retained the same Session, terminal answer, Work Ledger and artifact. Opening the artifact in the product showed the required marker, launch at `2026-09-07 09:00 Asia/Shanghai`, owner absence `10:00–12:00`, three-step handoff, rollback trigger `>2%` for at least five minutes, and a checkbox verification list.

## Durable execution evidence

- Tenant-scoped `app_rls` read-only PostgreSQL inspection found one run, RuntimeTask `9332f6e7-012f-576b-a6e1-70725a7415c3`, completed at `2026-09-06T12:39:00Z`.
- Five committed model rounds all recorded provider `zhipu`, model `glm-5.3`; the final round ended with `finish_reason=stop`. The five provider usage records total 234,493 tokens (222,489 prompt, 12,004 completion, 172,736 cached prompt, 8,970 reasoning); no authoritative currency cost was available.
- Thirteen tool invocations were durably recorded. Tool names were limited to `track_todo`, `record_finding`, `list_files`, `write_file`, and `read_file`; every invocation was `effect_committed`, `permission_state=not_required`, and had both receipt and result-event references.
- One owned Markdown artifact exists: `6339166d-6e8c-498f-bcbb-ef3c89d661d5`, path `workspace/P01-MAIN-PASS1-CEDAR-7K9M.md`, size 3790, snapshot hash `186065d4e80677e64a75fd4bc153f4b6582a518cfdba3cf8e519242d466089fc`, bound to the same RuntimeTask and final message.

## Earliest wrong state

- The run committed 1,033 transcript events faster than the background bridge could project them. At approximately `2026-09-06T12:51Z`, 118 contiguous events (`916–1033`) were still `pending` with zero attempts after more than 12 minutes; the sweeper was advancing roughly one event per five seconds.
- Required terminal outbox `7b200f1c-4d3f-5240-b344-e8706138aed7` exhausted eight attempts and became `dead_letter` with `last_error=WebTerminalBoundaryPending`, no delivery timestamp, and no settled completion timestamp on the RuntimeTask.
- Later read-only checks first found the sweeper at sequence 1011 with 22 pending events, then all 1,033 events projected. The same outbox remained `dead_letter`, `delivered_at` remained null, and the RuntimeTask still had no `completion_outbox_settled_at`. Complete natural drainage therefore does not restore the already exhausted terminal effect.
- The RuntimeTask is completed but still has an expired non-null claim. This is recorded as adjacent evidence; the terminal projection/dead-letter is the current earliest Journey blocker.

## Verdict and next action

P01-MAIN pass 1 is `Breakpoint`, not `PASS`: the user-visible task and artifact succeeded, but canonical terminal settlement and recovery did not. Pass 2, fault/recovery and authority-negative probes are intentionally not started. Repair the shared ordered bridge/outbox recovery path with failing-first coverage, complete independent review, deploy one coherent successor application commit, then use the existing operator redrive path exactly once for this dead letter without duplicating input/effects/final, and restart P01-MAIN pass 1 from a fresh Session.

No production mutation, redrive, retry, credential read, or protected content read was performed while collecting this evidence.
