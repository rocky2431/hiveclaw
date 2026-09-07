---
document_id: weekend-rc-2026-09-07-p01-main-pass-1-cc152f66
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: clean-pass-1-double-pass-negative-clean-session-delete-blocked
journey_id: P01-MAIN
environment: production
source_commit: cc152f6689a9bddc0310f24644e73b800ebb6df6
deployed_commit: cc152f6689a9bddc0310f24644e73b800ebb6df6
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: P01-MAIN-PASS1-CEDAR-FIR-20260907
started_at: 2026-09-07T06:30:28+08:00
ended_at: 2026-09-07T06:38:12+08:00
result: PASS
fault_recovery_result: PASS
negative_authority_result: PASS
cleanup_result: PARTIAL
---

# P01-MAIN production pass 1 on cc152f66

## Input and product consumption

- Ordinary employee CEDAR R2 created fresh Session `23dd3d47-0fe4-4372-839f-423b55c3aa64` for Agent `4e5261a6-c182-5248-9ca1-669f9419d44f` and submitted exactly one marker `P01-MAIN-PASS1-CEDAR-FIR-20260907`.
- The open task required a public plan, Work Ledger, an A/B release decision with four explicit trade-offs, one governed Markdown deliverable, readback, and externally checkable content. It prohibited external network, messages, other Agents, company knowledge, credentials, workflows, triggers, automations, and real business effects.
- GLM-5.3 completed in 4 minutes 50 seconds. The UI exposed the plan before file effects, selected staged option B, showed a 6/6 Work Ledger, one final answer, and one artifact. The final reported all requested content and the exact path after readback.
- The only deliverable was `workspace/P01-MAIN-PASS1-CEDAR-FIR-20260907.md`. Product UI showed the marker, fixed review time, maintenance window, four-dimensional A/B table, explicit choice with four reasons, three-step handoff, two quantitative rollback thresholds, responsibility matrix, and twelve-item checklist.
- A hard reload after canonical delivery restored the exact input, one final, 6/6 Ledger, GLM-5.3 status, and the same artifact without intervention.

## Durable execution evidence

- RuntimeTask `0ca797f7-bb52-5f07-9233-0e1edc6b0e71` is one completed `web_chat_turn` with `attempt_count=1`.
- All 1,389 transcript events are `projected`, spanning sequence 1 through 1,389. There is exactly one accepted human input and one completed assistant final.
- Five model rounds are all `round_committed` on provider `zhipu`, model `glm-5.3`. Every round contains the same 73 distinct authorized tools; the ordered surface digest is `b33e1a6c96810dbb6490fa0110b68ae9` in all five rounds.
- Twenty-one tool invocations are all `effect_committed` with `permission_state=not_required`: `track_todo` 14, `record_finding` 3, and one each of `list_files`, `write_file`, `read_file`, and `read_ledger`.
- Exactly one artifact is bound to the Session and run: `workspace/P01-MAIN-PASS1-CEDAR-FIR-20260907.md`, 8,779 bytes, MIME `text/markdown`, preview kind `markdown`, snapshot hash `d802642fe21ddf680d262abe559c5677634bcb0e6ff3975318091fe7542fa15b`.
- Required terminal outbox `1d90f244-b802-5099-a029-6bb48ac240bd` delivered naturally on attempt 1 with no error. Its receipt binds the same outbox as `boundary_id` and `t0_boundary_id`, terminal event `609d1d77-bd6d-498f-9d33-beee667e4177` at sequence 1,389, T0 event `evt_c4799f96cad543f1afae420a41308e69` at sequence 1,390, a non-empty response projection hash, summary through 1,389, and six canonical source references.

## Verdict

Pass 1 is clean for P01-MAIN on exact production application `cc152f66`: real employee persona, selected provider/model, complete authorized capability surface, governed effects, useful deliverable, natural terminal receipt, and hard-reload convergence all passed without administrator or console intervention.

P01-MAIN now has a clean second pass and clean fresh authority-negative. Seven exact workspace files are deleted. The owner confirmed permanent deletion of the seven corresponding Sessions, but the first supported DELETE timed out after 30.080 seconds and rolled back atomically; all seven remain. Cleanup is blocked on `SESSION-V2-DELETE-ORDER-001`, so NPTCR remains 0/96.
