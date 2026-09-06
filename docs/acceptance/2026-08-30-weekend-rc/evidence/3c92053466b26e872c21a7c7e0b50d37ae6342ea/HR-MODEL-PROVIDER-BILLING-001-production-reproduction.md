---
document_id: weekend-rc-2026-09-06-hr-model-provider-billing-production-reproduction
owner: Codex
status: active
authority: immutable-production-external-unavailability-evidence-not-nptcr-pass
last_reviewed: 2026-09-06
verification_status: external-unavailable-provider-balance-or-credit
journey_id: P13-HR / P01-MAIN precondition
environment: production
source_commit: 3c92053466b26e872c21a7c7e0b50d37ae6342ea
deployed_commit: 3c92053466b26e872c21a7c7e0b50d37ae6342ea
manifest_sha256: 73de9799eaf5b94970ad3b64b48fd8a19b9a24106ccee26212302f7c6a4c7e37
persona_principal: authenticated synthetic employee CEDAR R2 in the selected Weekend RC fixture tenant
data_version: WRC-P01-EMPLOYEE-AGENT-R1-20260906
started_at: 2026-09-06T20:04:00+08:00
ended_at: 2026-09-06T20:08:00+08:00
result: EXTERNAL_UNAVAILABLE
fault_recovery_result: NOT_RUN
negative_authority_result: NOT_RUN
cleanup_result: NOT_RUN
---

# HR model provider billing production reproduction

## Input and authority

- The owner entered the provider credential directly in the production UI. Codex did not read, receive, log, or copy it.
- Platform-admin readback showed `GLM-5.3`, provider/model `zhipu / glm-5.3`, enabled and default. System HR readback showed `glm-5.3 (zhipu)` selected after navigating away and back.
- Chrome remained authenticated as synthetic employee `WRC M1 Employee CEDAR R2` in tenant `0430e023-de03-4e8c-a3dc-b2a63e751427`.
- Codex created fresh HR Session `8544a582-e995-4adc-9ff0-e1f3d9e6f4d6` and submitted the same bounded creation brief exactly once. The input marker remained `WRC-P01-EMPLOYEE-AGENT-R1-20260906`.

## Earliest terminal state

- The Session showed `运行中 · GLM-5.3`, proving the missing-model gate was crossed and the selected model reached the runtime lane.
- After approximately 2 minutes 37 seconds, it terminalized as `失败 · GLM-5.3`.
- The exact user-facing error was `模型额度或余额不足，请联系管理员检查额度，或切换模型后重试。`
- The retry contract was `可重试`; Codex did not click `重试本轮`, create another Session, recharge, switch credential, or modify model configuration.
- No tool invocation, blueprint preview, confirmation, provisioning, or digital employee effect appeared.

## Verdict and recovery

- This is `EXTERNAL_UNAVAILABLE` for the configured provider credential, not a Journey PASS and not yet a demonstrated Hive code defect.
- P01-MAIN pass 1 still has not started because the employee does not yet have the required created Agent.
- Recovery requires the owner to restore usable balance/credit for the current Zhipu credential or configure and test another real provider/model through the supported production UI. Codex must not read, receive, log, or copy the credential and must not purchase or recharge.
- After the owner confirms provider readiness, Codex will create a fresh HR Session and submit the bounded brief once. The current retryable failed Session remains immutable evidence and a final-cleanup target.

## Readiness recovery follow-up

- A later fresh Session `3b0b01e8-3819-455d-bade-58a042323845` completed on GLM-5.3 and produced the expected canonical blueprint preview, proving current provider readiness had recovered.
- Session `8544a582-e995-4adc-9ff0-e1f3d9e6f4d6` was not retried. Its external-unavailability result remains immutable historical evidence and a final-cleanup target.
- The successful preview is recorded independently in `P01-HR-PREVIEW-001-production-verification.md`; it does not create a Journey PASS.

## Not proven

- HR blueprint preview/confirmation/provisioning, created Agent identity/model/tool surface, employee P01 pass 1, hard reload, pass 2, fault recovery, authority-negative behavior, cleanup, or any Journey PASS.
