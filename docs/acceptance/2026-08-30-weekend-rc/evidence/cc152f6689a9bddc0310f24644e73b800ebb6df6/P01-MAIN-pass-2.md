---
document_id: weekend-rc-2026-09-07-p01-main-pass-2-cc152f66
owner: Codex
status: active
authority: production-journey-evidence
last_reviewed: 2026-09-07
verification_status: clean-pass-2-negative-clean-session-delete-blocked
journey_id: P01-MAIN
environment: production
source_commit: cc152f6689a9bddc0310f24644e73b800ebb6df6
deployed_commit: cc152f6689a9bddc0310f24644e73b800ebb6df6
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: P01-MAIN-PASS2-CEDAR-GALE-20260907
started_at: 2026-09-07T06:50:02+08:00
ended_at: 2026-09-07T06:59:19+08:00
result: PASS
fault_recovery_result: PASS
negative_authority_result: PASS
cleanup_result: PARTIAL
---

# P01-MAIN production pass 2 on cc152f66

## Input and product consumption

- Ordinary employee CEDAR R2 created fresh Session `ba59a6e3-30a1-48bd-8b64-3402b70d1a7a` for Agent `4e5261a6-c182-5248-9ca1-669f9419d44f` and submitted exactly one marker `P01-MAIN-PASS2-CEDAR-GALE-20260907`.
- The distinct open task required a public plan, Work Ledger, a C/D night-drill staffing decision with four explicit trade-offs, one governed Markdown deliverable, readback, and nine externally checkable fact classes. It prohibited external network, messages, other Agents, company knowledge, credentials, workflows, triggers, automations, and real scheduling or drill effects.
- GLM-5.3 completed in 5 minutes 25 seconds. The UI exposed the plan before file effects, selected single-commander option C, showed a 5/5 Work Ledger, one final answer, and one artifact. The final reported all nine requested fact classes and the exact path after readback.
- The only deliverable was `workspace/P01-MAIN-PASS2-CEDAR-GALE-20260907.md`. Product UI showed the marker, fixed review time, drill window, four-dimensional C/D comparison, explicit choice with four reasons, four-step handoff, two quantitative abort thresholds, responsibility matrix, and ten-item checklist.
- A hard reload after canonical delivery restored the exact input, one final, 5/5 Ledger, GLM-5.3 status, and the same artifact without intervention.

## Durable execution evidence

- RuntimeTask `757f1651-4c7b-5656-820b-c571f7323cf2` is one completed `web_chat_turn` with `attempt_count=1`.
- All 1,754 transcript events are `projected`, spanning sequence 1 through 1,754. There is exactly one accepted human input and one completed assistant final.
- Seven model rounds are all `round_committed` on provider `zhipu`, model `glm-5.3`. Every round contains the same 73 distinct authorized tools; the ordered surface digest is `b33e1a6c96810dbb6490fa0110b68ae9` in all seven rounds.
- Twenty tool invocations are all `effect_committed` with `permission_state=not_required`: `track_todo` 12, `record_finding` 3, `read_ledger` 2, and one each of `list_files`, `write_file`, and `read_file`.
- Exactly one owned artifact is bound to the Session and run: `workspace/P01-MAIN-PASS2-CEDAR-GALE-20260907.md`, 8,041 bytes, MIME `text/markdown`, preview kind `markdown`, snapshot hash `cefb9e87c435925bae6d77b61b35429b4482cea2700ce3fdd1ae2abf9c6eef31`.
- Required terminal outbox `ba7d370f-0d5a-596f-a7fd-a694e4410528` delivered naturally on attempt 1 with no error. Its receipt binds the same outbox as `boundary_id` and `t0_boundary_id`, terminal event `3c84f475-9b12-48ab-b4a7-8f3d8744ecc1` at sequence 1,754, T0 event `evt_9ab1668e258c404bade1e25c320e3e4e` at sequence 1,755, a non-empty response projection hash, summary through 1,754, and six canonical source references.

## Verdict

Pass 2 is clean for P01-MAIN on exact production application `cc152f66`: a second fresh employee task, selected provider/model, complete authorized capability surface, governed effects, useful distinct deliverable, natural terminal receipt, and hard-reload convergence all passed without administrator or console intervention.

P01-MAIN now has two clean current-application passes and a clean fresh authority-negative. Seven exact workspace files are deleted. The owner confirmed permanent deletion of the seven corresponding Sessions, but the first supported DELETE timed out after 30.080 seconds and rolled back atomically; all seven remain. Cleanup is blocked on `SESSION-V2-DELETE-ORDER-001`, so NPTCR remains 0/96.
