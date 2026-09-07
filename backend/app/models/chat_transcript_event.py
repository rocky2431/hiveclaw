"""Transactional cloud event truth for replayable chat/session runs.

Committed rows drive run ordering, resume/fork/rewind, and typed UI projection.
Each row is projected exactly once into portable T0 Memory evidence through
``transcript_event_id`` / ``transcript_sequence`` join metadata.
"""

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


class ChatTranscriptEvent(Base):
    """One durable event in a replayable chat/session transcript."""

    __tablename__ = "chat_transcript_events"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_chat_transcript_events_session_sequence"),
        Index("ix_chat_transcript_events_session_sequence", "session_id", "sequence"),
        Index("ix_chat_transcript_events_run_id", "run_id"),
        Index("ix_chat_transcript_events_message_id", "message_id"),
        Index("ix_chat_transcript_events_listed_surface", "listed_surface"),
        CheckConstraint(
            "schema_version != 2 OR (item_id IS NOT NULL AND item_kind IS NOT NULL "
            "AND lifecycle IS NOT NULL AND payload_schema IS NOT NULL AND scope_json IS NOT NULL "
            "AND event_type = item_kind || '.' || lifecycle "
            "AND payload_schema = 'hive.session.payload.' || item_kind || '.' || lifecycle || '.v2')",
            name="ck_chat_transcript_events_v2_envelope",
        ),
        Index(
            "uq_chat_transcript_tool_result_invocation",
            "session_id",
            "invocation_id",
            unique=True,
            postgresql_where=text("schema_version = 2 AND item_kind = 'tool_result' AND lifecycle = 'completed'"),
            sqlite_where=text("schema_version = 2 AND item_kind = 'tool_result' AND lifecycle = 'completed'"),
        ),
        Index(
            "uq_chat_transcript_completion_causation",
            "session_id",
            "causation_id",
            "event_type",
            unique=True,
            postgresql_where=text(
                "causation_id IS NOT NULL AND event_type = 'agent_task_notification' "
                "AND metadata_json ? 'completion_outbox_id'"
            ),
            sqlite_where=text("causation_id IS NOT NULL AND event_type = 'agent_task_notification'"),
        ),
        Index(
            "uq_chat_transcript_budget_transition_causation",
            "session_id",
            "causation_id",
            "event_type",
            unique=True,
            postgresql_where=text(
                "causation_id IS NOT NULL AND event_type = 'runtime_budget_transition' "
                "AND metadata_json ? 'budget_transition_outbox_id'"
            ),
            sqlite_where=text("causation_id IS NOT NULL AND event_type = 'runtime_budget_transition'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runtime_tasks.id"), nullable=True)
    parent_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_transcript_events.id"), nullable=True, index=True
    )
    root_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=True
    )
    parent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id"), nullable=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    item_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lifecycle: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payload_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scope_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session_commands.id"), nullable=True, index=True
    )
    input_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    result_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    invocation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    provider_tool_use_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    item_type: Mapped[str] = mapped_column(String(64), nullable=False, default="event", server_default=text("'event'"))
    item_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="succeeded", server_default=text("'succeeded'")
    )
    turn_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    causation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="direct_user")
    listed_surface: Mapped[str] = mapped_column(String(64), nullable=False, default="chat")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    parts_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    projection_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default=text("'pending'"), index=True
    )
    projection_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    projection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# Keep isolated imports mapper-safe for append/replay services.
from app.models.audit import ChatMessage  # noqa: E402, F401
