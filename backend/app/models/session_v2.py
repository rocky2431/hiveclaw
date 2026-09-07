"""Durable authorities for the Session V2 event, command and recovery plane."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SessionEventCursor(Base):
    __tablename__ = "session_event_cursors"
    __table_args__ = (CheckConstraint("next_sequence > 0", name="ck_session_event_cursor_positive"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    next_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SessionEventOutbox(Base):
    __tablename__ = "session_event_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_session_event_outbox_event"),
        UniqueConstraint("session_id", "sequence", name="uq_session_event_outbox_session_sequence"),
        CheckConstraint(
            "status IN ('pending','publishing','published','failed')", name="ck_session_event_outbox_status"
        ),
        CheckConstraint("char_length(envelope_sha256) = 64", name="ck_session_event_outbox_sha"),
        Index("ix_session_event_outbox_claim", "status", "available_at", "claim_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_transcript_events.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    envelope_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    envelope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionCommand(Base):
    __tablename__ = "session_commands"
    __table_args__ = (
        Index("ix_session_commands_principal_type_id", "principal_type", "principal_id"),
        UniqueConstraint(
            "tenant_id",
            "principal_type",
            "principal_id",
            "session_id",
            "namespace",
            "idempotency_key",
            name="uq_session_commands_idempotency",
        ),
        CheckConstraint(
            "principal_type IN ('user','external_principal')",
            name="ck_session_commands_principal_type",
        ),
        CheckConstraint(
            "namespace IN ('human_input','control_input','evaluation_feedback','turn_replacement')",
            name="ck_session_commands_namespace",
        ),
        CheckConstraint(
            "status IN ('accepted','applied','rejected','failed','needs_reconciliation')",
            name="ck_session_commands_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    principal_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="user", server_default=text("'user'")
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    namespace: Mapped[str] = mapped_column(String(40), nullable=False)
    causation_command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_commands.id"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="accepted", server_default=text("'accepted'")
    )
    receipt_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    rejection_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SessionTurnInput(Base):
    __tablename__ = "session_turn_inputs"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_session_turn_inputs_command"),
        UniqueConstraint("session_id", "queue_priority", "queue_ordinal", name="uq_session_turn_inputs_fifo"),
        CheckConstraint(
            "intent IN ('start_turn','steer_current_turn','queue_next_turn','interrupt_and_replace','answer_request','fork_side_thread')",
            name="ck_session_turn_inputs_intent",
        ),
        CheckConstraint("queue_priority IN ('now','next','later')", name="ck_session_turn_inputs_priority"),
        CheckConstraint(
            "status IN ('accepted','queued','bound','applied','rolled_over','rejected','cancelled','needs_reconciliation')",
            name="ck_session_turn_inputs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_commands.id", ondelete="CASCADE"), nullable=False
    )
    intent: Mapped[str] = mapped_column(String(48), nullable=False)
    content_parts_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_turn_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), nullable=True
    )
    request_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    fork_after_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    terminal_fallback: Mapped[str | None] = mapped_column(String(32), nullable=True)
    queue_priority: Mapped[str] = mapped_column(String(16), nullable=False)
    queue_ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="accepted", server_default=text("'accepted'")
    )
    bound_round_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_request_snapshot_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    rolled_over_to_turn_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    settlement_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    recovery_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SessionInputAdmission(Base):
    __tablename__ = "session_input_admissions"
    __table_args__ = (
        UniqueConstraint(
            "input_id",
            "input_revision",
            name="uq_session_input_admissions_input_revision",
        ),
        UniqueConstraint("hook_run_id", name="uq_session_input_admissions_hook_run"),
        CheckConstraint(
            "state IN ('admission_pending','hook_running','hook_result_committed','admitted','rejected','cancelled','needs_reconciliation')",
            name="ck_session_input_admissions_state",
        ),
        CheckConstraint(
            "dispatch_state IN ('not_applicable','pending','dispatching','dispatched','needs_reconciliation')",
            name="ck_session_input_admissions_dispatch_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_commands.id", ondelete="CASCADE"), nullable=False
    )
    input_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_turn_inputs.id", ondelete="CASCADE"), nullable=False
    )
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, default="admission_pending", server_default=text("'admission_pending'")
    )
    hook_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    hook_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    hook_result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hook_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    additional_context_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    carry_forward: Mapped[str] = mapped_column(
        String(40), nullable=False, default="none", server_default=text("'none'")
    )
    dispatch_state: Mapped[str] = mapped_column(
        String(40), nullable=False, default="not_applicable", server_default=text("'not_applicable'")
    )
    dispatch_receipt_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    dispatch_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SessionCarryForward(Base):
    __tablename__ = "session_carry_forwards"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_admission_id", "purpose", name="uq_session_carry_forward_source"),
        UniqueConstraint("tenant_id", "context_source_item_id", name="uq_session_carry_forward_context_item"),
        CheckConstraint(
            "state IN ('pending','turn_claimed','round_bound','consumed','needs_reconciliation')",
            name="ck_session_carry_forward_state",
        ),
        CheckConstraint(
            "state <> 'consumed' OR (target_turn_id IS NOT NULL AND target_round_id IS NOT NULL "
            "AND model_request_snapshot_ref IS NOT NULL AND consumed_event_id IS NOT NULL)",
            name="ck_session_carry_forward_consumed",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, default="prevented_prompt_context")
    source_admission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_input_admissions.id", ondelete="CASCADE"), nullable=False
    )
    source_input_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_turn_inputs.id", ondelete="CASCADE"), nullable=False
    )
    source_hook_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_evidence_refs_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    context_source_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default=text("'pending'"))
    target_turn_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_round_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    claim_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_request_snapshot_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    consumed_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_transcript_events.id"), nullable=True, index=True
    )
    recovery_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


class SessionControlInput(Base):
    __tablename__ = "session_control_inputs"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_session_control_inputs_command"),
        CheckConstraint(
            "kind IN ('cancel_run','approval_response','permission_response','workflow_gate_response')",
            name="ck_session_control_inputs_kind",
        ),
        CheckConstraint(
            "status IN ('accepted','applying','applied','rejected','failed','needs_reconciliation')",
            name="ck_session_control_inputs_status",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_commands.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    expected_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), nullable=False
    )
    request_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authority_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_schema: Mapped[str | None] = mapped_column(String(300), nullable=True)
    response_payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    response_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="accepted", server_default=text("'accepted'")
    )
    settlement_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    recovery_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


class SessionTurnReplacement(Base):
    __tablename__ = "session_turn_replacements"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_session_turn_replacements_command"),
        UniqueConstraint("replacement_turn_id", name="uq_session_turn_replacements_turn"),
        CheckConstraint(
            "state IN ('requested','cancel_accepted','old_run_fenced','replacement_queued','replacement_admitted','completed','failed','needs_reconciliation')",
            name="ck_session_turn_replacements_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_commands.id", ondelete="CASCADE"), nullable=False
    )
    old_turn_id: Mapped[str] = mapped_column(String(200), nullable=False)
    old_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), nullable=False)
    cancel_control_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    cancel_command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_commands.id"), nullable=True
    )
    replacement_turn_id: Mapped[str] = mapped_column(String(200), nullable=False)
    replacement_input_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_turn_inputs.id"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, default="requested", server_default=text("'requested'")
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_transcript_events.id"), nullable=True, index=True
    )


class SessionToolInvocation(Base):
    __tablename__ = "session_tool_invocations"
    __table_args__ = (
        UniqueConstraint("provider_request_id", "provider_tool_use_id", name="uq_session_tool_provider_mapping"),
        UniqueConstraint("effect_idempotency_key", name="uq_session_tool_effect_key"),
        CheckConstraint(
            "effect_state IN ('prepared_not_started','effect_started','effect_committed','failed','needs_reconciliation')",
            name="ck_session_tool_effect_state",
        ),
        CheckConstraint(
            "permission_state IN ('not_required','waiting','approved','denied','expired','cancelled')",
            name="ck_session_tool_permission_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_request_id: Mapped[str] = mapped_column(String(300), nullable=False)
    provider_tool_use_id: Mapped[str] = mapped_column(String(300), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default=text("''"))
    provider_arguments_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    invocation_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_arguments_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    effective_args_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authority_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    effect_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prepared_not_started", server_default=text("'prepared_not_started'")
    )
    execution_fence_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    receipt_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    result_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_transcript_events.id"), nullable=True, unique=True
    )
    recovery_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    permission_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    permission_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_required", server_default=text("'not_required'")
    )
    permission_request_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    permission_authority_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permission_response_schema: Mapped[str | None] = mapped_column(String(300), nullable=True)
    permission_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    permission_receipt_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


class SessionModelResult(Base):
    __tablename__ = "session_model_results"
    __table_args__ = (
        UniqueConstraint("provider_request_id", name="uq_session_model_results_provider_request"),
        UniqueConstraint("run_id", "round_id", name="uq_session_model_results_round"),
        CheckConstraint(
            "state IN ('prepared','streaming','sealed','round_committed','failed','needs_reconciliation')",
            name="ck_session_model_results_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    turn_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_request_id: Mapped[str] = mapped_column(String(300), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prepared", server_default=text("'prepared'")
    )
    model_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_request_snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    bound_input_ids_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    last_content_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    seal_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    round_committed_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_transcript_events.id"), nullable=True, index=True
    )
    reconciliation_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reconciliation_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


class SessionRoundObligation(Base):
    __tablename__ = "session_round_obligations"
    __table_args__ = (
        UniqueConstraint(
            "source_result_id", "kind", "source_generation", "source_ref", name="uq_session_round_obligation_source"
        ),
        CheckConstraint(
            "kind IN ('tool_followup','pending_input','hook_retry','compact_continue')",
            name="ck_session_round_obligation_kind",
        ),
        CheckConstraint(
            "state IN ('pending','claimed','settled','needs_reconciliation')", name="ck_session_round_obligation_state"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    turn_id: Mapped[str] = mapped_column(String(200), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False
    )
    source_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_model_results.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    source_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default=text("'pending'"))
    claim_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settlement_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    recovery_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


class SessionNextRoundPlan(Base):
    __tablename__ = "session_next_round_plans"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "next_round_id",
            "plan_generation",
            name="uq_session_next_round_plan_generation",
        ),
        UniqueConstraint(
            "run_id",
            "next_round_id",
            "plan_hash",
            name="uq_session_next_round_plan_hash",
        ),
        Index(
            "uq_session_next_round_plan_current",
            "run_id",
            "next_round_id",
            unique=True,
            postgresql_where=text("state IN ('committed','dispatched','needs_reconciliation')"),
        ),
        CheckConstraint(
            "state IN ('prepared','committed','dispatched','abandoned','needs_reconciliation')",
            name="ck_session_next_round_plan_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False
    )
    source_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_model_results.id", ondelete="CASCADE"), nullable=False
    )
    next_round_id: Mapped[str] = mapped_column(String(200), nullable=False)
    plan_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    obligation_ids_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    ordered_sources_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    fences_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prepared", server_default=text("'prepared'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


class SessionRunOutcome(Base):
    __tablename__ = "session_run_outcomes"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_session_run_outcomes_run"),
        CheckConstraint(
            "state IN ('prepared','sealed','terminal_committed','failed','needs_reconciliation')",
            name="ck_session_run_outcomes_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    turn_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False
    )
    terminal_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_model_results.id"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prepared", server_default=text("'prepared'")
    )
    eligibility_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    seal_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    terminal_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_transcript_events.id"), nullable=True, index=True
    )
    reconciliation_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reconciliation_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


class SessionFeedbackAggregate(Base):
    __tablename__ = "session_feedback_aggregates"
    __table_args__ = (
        CheckConstraint("status IN ('active','withdrawn')", name="ck_session_feedback_aggregate_status"),
        UniqueConstraint("tenant_id", "session_id", "id", name="uq_session_feedback_aggregate_scope"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_result_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    current_value_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", server_default=text("'active'"))
    last_mutation_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SessionWriterEpoch(Base):
    __tablename__ = "session_writer_epochs"
    __table_args__ = (
        CheckConstraint("state IN ('legacy_open','v1_draining','v2_only')", name="ck_session_writer_epoch_state"),
        CheckConstraint("enforcement_mode IN ('observe','enforce')", name="ck_session_writer_epoch_enforcement"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default="global")
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="legacy_open", server_default=text("'legacy_open'")
    )
    new_run_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    allowed_existing_generations_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: [1])
    enforcement_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="observe", server_default=text("'observe'")
    )
    release_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SessionWriterHeartbeat(Base):
    __tablename__ = "session_writer_heartbeats"
    __table_args__ = (UniqueConstraint("service", "instance_id", name="uq_session_writer_heartbeat_instance"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service: Mapped[str] = mapped_column(String(40), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(200), nullable=False)
    artifact_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    supported_generations_json: Mapped[list] = mapped_column(JSONB, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
