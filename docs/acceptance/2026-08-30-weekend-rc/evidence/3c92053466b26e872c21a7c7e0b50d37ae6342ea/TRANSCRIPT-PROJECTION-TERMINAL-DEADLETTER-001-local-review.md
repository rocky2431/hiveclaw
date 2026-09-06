---
document_id: weekend-rc-2026-09-06-p01-terminal-projection-local-review
owner: Codex
status: active
authority: local-review-evidence-not-production
last_reviewed: 2026-09-06
verification_status: local-review-complete-exact-ci-pending
finding_id: TRANSCRIPT-PROJECTION-TERMINAL-DEADLETTER-001
source_commit: 3c92053466b26e872c21a7c7e0b50d37ae6342ea
result: ACCEPTED_LOCAL_CANDIDATE
---

# P01 terminal projection local repair review

## Scope and root cause

Production P01 Session `3351fede-216a-44d5-9dfc-21cce7313356` completed its model, tools, final and artifact, but its required terminal outbox `7b200f1c-4d3f-5240-b344-e8706138aed7` dead-lettered before the transcript prefix drained. The old bridge consumed one predecessor per outer retry: a single call could project at most 39 predecessors, while the eight-attempt outbox envelope could cover at most 312. The production run still had 118 of 1,033 events pending after that envelope was exhausted.

The candidate changes only the shared bridge: it loads the committed unfinished predecessor IDs in sequence order and bridges them one at a time, stopping at the first failure. It does not add a queue, increase an attempt cap, widen RLS, or bypass a failed frontier.

## Independent review

- zCode delegation `6e50aca24985440fa7ac80abe4719b7c` produced source SHA-256 `bb13510c6b6d1c24cdc9719e2cb5cb3c7b3bb4ce794e30dd3395226adb632d0b` and initial test SHA-256 `aeceedcd684d3ab3dea75a1e86203f1c3d12c7fa02e078b48db68eb137b19f19`. Its configured default and worker report identified GLM-5.3; the ACP receipt did not independently expose provider model-I/O telemetry, so this is not claimed as stronger model proof.
- CC delegation `4fc7c994a81a4c0ab6af63130d4a5e54` kept both frozen hashes unchanged and returned `ACCEPTED`. It ran the candidate suite as 14 passed, restored the old one-predecessor behavior with an external runtime patch and obtained 2 failed / 12 passed, then ran 75 adjacent tests. It also proved that the new fail-closed test duplicated an existing real-PG fixture.
- Codex replaced that duplicated setup with the existing `_prepare_boundary_mismatch_session`; the assertions remain frontier failed, later row pending with zero attempts, and T0 unchanged. The final test SHA-256 is `d181503a3e032a73bbe40872ff290716bac969d3102b657d8541ca80361e39f6`.

## Codex verification

- Restoring the old source semantics made the two backlog/outbox regressions fail while the fail-closed invariant passed: `2 failed / 1 passed`. Restoring the candidate made the same set `3 passed` and returned the source hash to `bb13510c…`.
- Final candidate and adjacent suites: transcript/bridge/terminal processors `89 passed`; runtime task worker `32 passed`; Weekend RC document structure `10 passed`; Ruff, format and diff checks passed.
- A temporary real-PostgreSQL outbox probe raised the backlog from 90 to 1,033. The real `RuntimeTerminalBoundaryOutboxService` and `WebTerminalBoundaryProcessor` delivered in 78.06 seconds; the callback crossed the 60-second initial lease and retained its fence through the existing renewal loop. The probe was then restored to the proportionate 90-row committed regression.
- A temporary concurrent probe ran the terminal drain and transcript sweeper against the same 61-row Session. It passed with every row projected once, every attempt count equal to one, and exact T0 order without duplicates. The probe was removed after execution.
- The RLS fingerprint changed from `ba6aa841…` to `94a52136…`. Independent analyzer comparison found 584 digest entries before and after, 109 callsites/signatures before and after, no callsite delta, and exactly one digest replacement: `app/services/runtime_control_bus.py` `<module-source>`. After registering `94a521369198f1beb9d548139d2caa54921b6576fcc2e3912ef72d650f93eb64`, the allowlist suite passed 17/17.

The 1,033-row probe establishes correctness and lease survival, not a throughput target: full-ledger replay remains quadratic and took 78.06 seconds on this host. It is acceptable for this recovery path because the existing claim renewer preserves the fence and normal inline projection prevents such a prefix; optimize only if production evidence shows recurring large-prefix latency or tenant starvation.

## Recovery decision

The code prevents the same recoverable backlog from exhausting a new terminal outbox, but it deliberately does not revive existing dead letters. The repository already has an operator-authorized, audited, exact-row redrive API. After exact CI and same-source deployment, read the current summary projection state, invoke that API once for `7b200f1c-4d3f-5240-b344-e8706138aed7`, and verify one audit, one delivery receipt, RuntimeTask settlement, and no duplicated input, tool effect, artifact, or final. Do not add automatic dead-letter revival.

This local review is not production verification and does not change P01 from `Breakpoint` or NPTCR from 0/96.
