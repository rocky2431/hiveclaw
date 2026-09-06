---
document_id: weekend-rc-2026-09-07-p01-main-pass-2
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: clean-pass-2-negative-and-cleanup-pending
journey_id: P01-MAIN
environment: production
source_commit: 17fed53097457cb23e52bb6545752198926c93ed
deployed_commit: 17fed53097457cb23e52bb6545752198926c93ed
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: P01-MAIN-PASS2-CEDAR-BIRCH-20260907
started_at: 2026-09-07T01:37:54+08:00
ended_at: 2026-09-07T01:49:06+08:00
result: PASS
fault_recovery_result: PASS
negative_authority_result: NOT_RUN
cleanup_result: NOT_RUN
---

# P01-MAIN production pass 2

## Input and product consumption

- Ordinary employee CEDAR R2 created fresh Session `2043d7d5-21c5-4eba-a58c-4ee6645c1765` for Agent `4e5261a6-c182-5248-9ca1-669f9419d44f` and submitted exactly one marker `P01-MAIN-PASS2-CEDAR-BIRCH-20260907`.
- The independent open task required a public plan, Work Ledger, C/D decision with explicit trade-offs, one governed Markdown deliverable, eight externally checkable facts and write-then-read verification. It prohibited any real switch, external network, messages, other Agents, company knowledge, credentials, workflows, triggers and automations.
- The UI exposed the model-authored plan before file effects, a 6/6 Work Ledger, one final answer and one artifact. It completed in 4 minutes 51 seconds on GLM-5.3.
- The final selected strategy D and explicitly disclosed its write-path and handoff weaknesses, mitigations and the condition for reconsidering C. Product preview recovered the marker, fixed time and absence window, eight-dimension comparison, explicit decision, four-step handoff, two exact abort thresholds, responsibility matrix and ten-item checklist.
- A hard reload before terminal delivery and another after the durable receipt both converged without intervention to the exact input, one final, 6/6 Ledger and one usable artifact.

## Durable execution evidence

- RuntimeTask `bfb641c6-57f6-50a9-9faa-393d407ab353` ran once and completed; run outcome is `terminal_committed` with terminal result `ae8c702b-ccae-5c8e-95b4-10d0e97327a0`.
- All 1,617 transcript events are `projected`. There is exactly one accepted input, one completed final, one completed turn and one completed run.
- Five model rounds are all `round_committed` and record provider `zhipu`, model `glm-5.3`. Every round exposed the same complete authorized surface of 73 named tools.
- Eighteen tool invocations are all `effect_committed`, have result events and use `permission_state=not_required`: `track_todo` 14 and one each of `glob_search`, `write_file`, `read_file` and `record_finding`.
- Exactly one artifact is bound to the Session: `workspace/P01-MAIN-PASS2-CEDAR-BIRCH-20260907.md`, size 8,484 bytes, snapshot hash `ae04253785257a6fdd604c73ac33f3fb9c024b77a178029ba08d016cbf348ef9`.
- Required terminal outbox `93f7ce97-f3d2-54f5-aff6-2f4364f028b2` delivered naturally on attempt 1 with no error. Its receipt binds the same outbox as `boundary_id`/`t0_boundary_id`, terminal event `a829a634-ed1e-4983-a989-95b07d6047bd`, T0 event `evt_3b16d03f975f4993901bd35a7d2013bb` at sequence 1,618, and summary through 1,617 from six canonical references.

## Verdict

Pass 2 is clean for the frozen P01-MAIN task on exact production commit `17fed530`. Together with pass 1, this establishes two fresh employee open-task passes with the selected model, complete authorized capability surface, governed effects, useful deliverables, terminal receipts and hard-reload convergence.

P01-MAIN is not yet `Closed loop`: its authority-negative column and final cleanup remain pending. NPTCR therefore remains 0/96.
