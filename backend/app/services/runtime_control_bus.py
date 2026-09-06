from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import get_redis
from app.runtime.hooks import HookEvent, emit_hook


RUNTIME_CONTROL_SCHEMA = "hive.runtime.control.v1"
RUNTIME_CONTROL_CHANNEL = "hive:runtime:control"
RUNTIME_CONTROL_RECONNECT_SECONDS = 2.0
RUNTIME_CONTROL_PUBLISH_TIMEOUT_SECONDS = 2.0
TRANSCRIPT_PROJECTION_SWEEP_SECONDS = 5.0
SESSION_LIFECYCLE_MESSAGE_LIMIT = 100
_STATE: dict[str, Any] = {
    "running": False,
    "received": 0,
    "last_type": None,
    "last_error": None,
    "restart_count": 0,
    "last_restart_at": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def publish_runtime_control_event(payload: dict[str, Any]) -> None:
    envelope = {
        "schema": RUNTIME_CONTROL_SCHEMA,
        "created_at": _now_iso(),
        **payload,
    }
    redis = await get_redis()
    # Publishing is an advisory wake-up, never the transactional truth. A
    # stalled Redis socket must not keep a DB transaction (and its transcript /
    # RuntimeTask row locks) open indefinitely; the projection sweeper recovers
    # committed pending rows when this bounded publish fails.
    await asyncio.wait_for(
        redis.publish(RUNTIME_CONTROL_CHANNEL, json.dumps(envelope, ensure_ascii=False, default=str)),
        timeout=RUNTIME_CONTROL_PUBLISH_TIMEOUT_SECONDS,
    )


async def publish_web_chat_cancel(
    *,
    run_id: str | Any,
    agent_id: str | Any,
    session_id: str | Any,
    user_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "web_chat_cancel",
            "run_id": str(run_id).replace("-", ""),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "user_id": str(user_id) if user_id is not None else None,
        }
    )


async def publish_business_task_cancel(
    *,
    task_id: str | Any,
    runtime_task_id: str | Any,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "business_task_cancel",
            "task_id": str(task_id),
            "runtime_task_id": str(runtime_task_id),
        }
    )


async def publish_delegation_cancel(
    *,
    task_id: str,
    parent_agent_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "delegation_cancel",
            "task_id": str(task_id),
            "parent_agent_id": str(parent_agent_id) if parent_agent_id is not None else None,
        }
    )


async def publish_subagent_cancel(
    *,
    run_id: str | Any,
    parent_agent_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "subagent_cancel",
            "run_id": str(run_id).replace("-", ""),
            "parent_agent_id": str(parent_agent_id) if parent_agent_id is not None else None,
        }
    )


async def publish_transcript_t0_bridge(
    *,
    transcript_event_id: str | Any,
    agent_id: str | Any,
    session_id: str | Any,
    tenant_id: str | Any | None = None,
) -> None:
    await publish_runtime_control_event(
        {
            "type": "transcript_t0_bridge",
            "transcript_event_id": str(transcript_event_id),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
        }
    )


async def publish_session_lifecycle_hook(
    *,
    event: str | HookEvent,
    agent_id: str | Any,
    session_id: str | Any | None,
    messages: list[dict[str, Any]] | None = None,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    event_value = event.value if isinstance(event, HookEvent) else str(event)
    clean_metadata = dict(metadata or {})
    if messages is not None:
        clean_metadata.setdefault("message_count", len(messages))
    await publish_runtime_control_event(
        {
            "type": "session_lifecycle_hook",
            "event": event_value,
            "agent_id": str(agent_id),
            "session_id": str(session_id) if session_id is not None else None,
            "source": source,
            "metadata": clean_metadata,
        }
    )


def _uuid_or_none(value: str | Any | None) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _load_session_lifecycle_messages(
    *,
    agent_id: str | Any | None,
    session_id: str | Any | None,
    limit: int = SESSION_LIFECYCLE_MESSAGE_LIMIT,
) -> list[dict[str, Any]]:
    if not session_id:
        return []

    from app.database import async_session, tenant_scoped_session
    from app.models.audit import ChatMessage
    from app.services.tenant_resolver import resolve_tenant_for_chat_session

    tenant_id = await resolve_tenant_for_chat_session(
        session_id,
        session_factory=async_session,
    )
    if tenant_id is None:
        return []

    filters = [
        ChatMessage.tenant_id == tenant_id,
        ChatMessage.conversation_id == str(session_id),
    ]
    agent_uuid = _uuid_or_none(agent_id)
    if agent_uuid is not None:
        filters.append(ChatMessage.agent_id == agent_uuid)

    async with tenant_scoped_session(
        tenant_id,
        session_factory=async_session,
        require_tenant=True,
        source="runtime_control_bus.lifecycle_messages",
    ) as db:
        result = await db.execute(
            select(ChatMessage).where(*filters).order_by(ChatMessage.created_at.desc()).limit(max(1, limit))
        )
        rows = list(result.scalars().all())

    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows if row.content is not None]


async def _unprojected_predecessor_transcript_event_ids(
    db: AsyncSession,
    *,
    transcript_event: Any,
) -> list[UUID]:
    """Return every unfinished predecessor, earliest first.

    Per-session transcript order is the cloud truth. Projecting a later row
    first would make the portable T0 artifact disagree with that truth, so a
    worker drains predecessors before touching the requested row. The whole
    committed unfinished prefix is returned in one ordered pass so a single
    bridge call can drain a backlog of any size.
    """
    if not isinstance(db, AsyncSession):
        return []
    from app.models.chat_transcript_event import ChatTranscriptEvent

    result = await db.execute(
        select(ChatTranscriptEvent.id)
        .where(
            ChatTranscriptEvent.session_id == transcript_event.session_id,
            ChatTranscriptEvent.sequence < transcript_event.sequence,
            ChatTranscriptEvent.projection_status.in_(("pending", "failed", "projecting")),
        )
        .order_by(ChatTranscriptEvent.sequence.asc())
    )
    return list(result.scalars())


# Deterministic prefix for the mechanical seal of a terminal owner that has no
# canonical terminal-boundary lane (plain subagent). The full key is
# ``{prefix}:{runtime_task_id}``, so replays reuse the original boundary
# receipt instead of sealing a second time.
TERMINAL_OWNER_SEGMENT_RECOVERY_IDEMPOTENCY_KEY = "t0-terminal-owner-recovery"


async def _seal_terminal_boundary_pending_owner(
    db: AsyncSession,
    *,
    transcript_event: Any,
    boundary_error: Any,
) -> bool:
    """Mechanically seal a stale open segment whose terminal owner cannot.

    Plain ``subagent`` terminal tasks emit T0-bridged transcript events but are
    not in ``TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES``, so no canonical terminal
    boundary outbox will ever seal the segment they opened and every later
    task's events fail on the owner mismatch. This shared mismatch boundary
    proves the exact owner is terminal, completed, tenant/agent/session-scoped
    to this transcript event, and carries no canonical boundary generation,
    then seals only that owner's segment. It is content-free mechanical
    recovery: the RuntimeTask row is never mutated and no semantic success is
    invented. Any unproven fact returns False so the caller fails closed.
    """

    from app.memory.t0.ledger import seal_t0_session_segment
    from app.models.runtime_task import TERMINAL_BOUNDARY_TERMINAL_STATUSES, RuntimeTask

    owner_uuid = _uuid_or_none(getattr(boundary_error, "active_runtime_task_id", None))
    if owner_uuid is None:
        return False
    owner_task = (
        await db.execute(select(RuntimeTask).where(RuntimeTask.id == owner_uuid).with_for_update())
    ).scalar_one_or_none()
    if owner_task is None:
        return False
    owner_agent_ids = {owner_task.parent_agent_id, owner_task.child_agent_id}
    owner_session_ids = {
        _uuid_or_none(owner_task.parent_session_id),
        _uuid_or_none(owner_task.child_session_id),
    }
    if (
        owner_task.tenant_id != transcript_event.tenant_id
        or transcript_event.agent_id not in owner_agent_ids
        or transcript_event.session_id not in owner_session_ids
        or owner_task.status not in TERMINAL_BOUNDARY_TERMINAL_STATUSES
        or owner_task.completed_at is None
        or owner_task.terminal_boundary_generation is not None
        or owner_task.terminal_boundary_enqueued_at is not None
    ):
        return False
    seal_t0_session_segment(
        agent_id=transcript_event.agent_id,
        session_id=transcript_event.session_id,
        reason="terminal_owner_segment_recovery",
        metadata={
            "recovery": "terminal_nonboundary_owner",
            "owner_task_type": owner_task.task_type,
            "owner_status": owner_task.status,
            "owner_completed_at": owner_task.completed_at.isoformat(),
            "recovered_for_transcript_event": str(transcript_event.id),
            "incoming_runtime_task_id": getattr(boundary_error, "incoming_runtime_task_id", None),
            "active_segment_id": getattr(boundary_error, "active_segment_id", None),
        },
        idempotency_key=f"{TERMINAL_OWNER_SEGMENT_RECOVERY_IDEMPOTENCY_KEY}:{owner_uuid}",
        expected_runtime_task_id=owner_uuid,
    )
    return True


async def _incoming_transcript_run_is_orphan(
    db: AsyncSession,
    *,
    transcript_event: Any,
    boundary_error: Any,
) -> bool:
    """Whether the mismatching incoming run can never own a T0 segment.

    A committed transcript row can outlive its RuntimeTask (the task row is
    deleted while the transcript truth remains).  Such a run has no turn
    identity: no writer can enqueue a canonical terminal boundary for it and
    the nonboundary seal helper cannot prove owner facts about a task that
    does not exist, so waiting on a seal can never terminate.  Returning True
    lets the caller project the row as a guest of the session's active
    segment instead; any run with a live RuntimeTask row keeps the
    fail-closed owner mismatch.
    """

    if not isinstance(db, AsyncSession):
        return False
    incoming_uuid = _uuid_or_none(getattr(boundary_error, "incoming_runtime_task_id", None))
    if incoming_uuid is None or transcript_event.tenant_id is None:
        return False
    from app.models.runtime_task import RuntimeTask

    found = await db.scalar(
        select(RuntimeTask.id)
        .where(
            RuntimeTask.id == incoming_uuid,
            RuntimeTask.tenant_id == transcript_event.tenant_id,
        )
        .limit(1)
    )
    return found is None


async def _incoming_transcript_run_is_terminal_nonboundary(
    db: AsyncSession,
    *,
    transcript_event: Any,
    boundary_error: Any,
) -> bool:
    """Whether the mismatching incoming run is terminal with no boundary lane.

    A terminal task of a type that never enters the canonical terminal
    boundary outbox (plain ``subagent``) can neither seal the active segment
    nor wait on a canonical boundary of its own, so the caller projects it as
    a guest event instead. Any unproven fact returns False so the caller
    fails closed.
    """

    if not isinstance(db, AsyncSession):
        return False
    incoming_uuid = _uuid_or_none(getattr(boundary_error, "incoming_runtime_task_id", None))
    if incoming_uuid is None or transcript_event.tenant_id is None:
        return False
    from app.models.runtime_task import TERMINAL_BOUNDARY_TERMINAL_STATUSES, RuntimeTask

    incoming_task = (
        await db.execute(
            select(RuntimeTask)
            .where(
                RuntimeTask.id == incoming_uuid,
                RuntimeTask.tenant_id == transcript_event.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    return incoming_task is not None and (
        transcript_event.agent_id in {incoming_task.parent_agent_id, incoming_task.child_agent_id}
        and transcript_event.session_id
        in {_uuid_or_none(incoming_task.parent_session_id), _uuid_or_none(incoming_task.child_session_id)}
        and incoming_task.status in TERMINAL_BOUNDARY_TERMINAL_STATUSES
        and incoming_task.completed_at is not None
        and incoming_task.terminal_boundary_generation is None
        and incoming_task.terminal_boundary_enqueued_at is None
    )


async def bridge_transcript_event_to_t0(
    *,
    transcript_event_id: str | Any,
    attempts: int = 40,
    retry_delay_seconds: float = 0.25,
) -> bool:
    event_uuid = _uuid_or_none(transcript_event_id)
    if event_uuid is None:
        _STATE["last_error"] = f"ValueError: invalid transcript_event_id {transcript_event_id!r}"
        return False

    from app.database import async_session, tenant_scoped_session
    from app.memory.t0.ledger import T0SegmentBoundaryPending, append_t0_session_event, replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.tenant_resolver import resolve_tenant_for_transcript_event

    attempts = max(1, attempts)
    for attempt in range(attempts):
        predecessor_event_ids: list[UUID] = []
        projection_failed = False
        tenant_id = await resolve_tenant_for_transcript_event(
            event_uuid,
            session_factory=async_session,
        )
        if tenant_id is not None:
            async with tenant_scoped_session(
                tenant_id,
                session_factory=async_session,
                require_tenant=True,
                source="runtime_control_bus.transcript_t0_bridge",
            ) as db:
                result = await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.id == event_uuid,
                        ChatTranscriptEvent.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                transcript_event = result.scalar_one_or_none()
                if transcript_event is not None:
                    metadata = dict(transcript_event.metadata_json or {})
                    if (
                        metadata.get("t0_bridge_relayed_at")
                        or getattr(transcript_event, "projection_status", None) == "projected"
                    ):
                        return True

                    predecessor_event_ids = await _unprojected_predecessor_transcript_event_ids(
                        db,
                        transcript_event=transcript_event,
                    )
                    if not predecessor_event_ids:
                        transcript_event.projection_status = "projecting"
                        transcript_event.projection_attempts = (
                            int(getattr(transcript_event, "projection_attempts", 0) or 0) + 1
                        )
                        transcript_event.projection_error = None

                        chat_message = None
                        if transcript_event.message_id:
                            chat_message = await db.get(ChatMessage, transcript_event.message_id)
                        actor_id = getattr(chat_message, "user_id", None) or transcript_event.agent_id
                        role = metadata.get("t0_role") or metadata.get("role")
                        source = str(metadata.get("source") or "runtime_control")

                        existing_t0_event = next(
                            (
                                event
                                for event in replay_t0_session_events(
                                    agent_id=transcript_event.agent_id,
                                    session_id=transcript_event.session_id,
                                )
                                if str(event.metadata.get("transcript_event_id") or "") == str(event_uuid)
                            ),
                            None,
                        )

                        def _append_t0_event(*, join_active_segment: bool = False) -> Any:
                            append_metadata = metadata
                            append_run_id = transcript_event.run_id
                            if join_active_segment:
                                # A guest run — no RuntimeTask row, or a proven
                                # terminal task with no boundary lane of its
                                # own — cannot open, own, or boundary a
                                # segment, so it projects as a guest event of
                                # the active segment. The FK on run_id means
                                # a run identity rides metadata when the
                                # column is NULL, so record the same
                                # column-or-metadata identity the ledger used
                                # for the failed match.
                                append_metadata = {
                                    key: value
                                    for key, value in metadata.items()
                                    if key not in ("runtime_task_id", "turn_id")
                                }
                                original_run_id = transcript_event.run_id or metadata.get("runtime_task_id")
                                if original_run_id is not None:
                                    append_metadata["t0_bridge_joined_segment_run_id"] = str(original_run_id)
                                if metadata.get("turn_id"):
                                    append_metadata["t0_bridge_joined_segment_turn_id"] = str(metadata["turn_id"])
                                append_run_id = None
                            return append_t0_session_event(
                                agent_id=transcript_event.agent_id,
                                session_id=transcript_event.session_id,
                                event_type=transcript_event.event_type,
                                role=str(role) if role else None,
                                content=transcript_event.content or "",
                                message_id=transcript_event.message_id,
                                actor_id=actor_id,
                                tenant_id=transcript_event.tenant_id,
                                runtime_task_id=append_run_id,
                                source=source,
                                metadata=append_metadata,
                                created_at=transcript_event.created_at,
                            )

                        try:
                            if existing_t0_event is None:
                                try:
                                    t0_result = _append_t0_event()
                                except T0SegmentBoundaryPending as boundary_error:
                                    # Three disjoint recovery cases; every
                                    # unproven fact still re-raises and fails
                                    # closed, and each retry is the single
                                    # recovery attempt. First, a run with no
                                    # RuntimeTask row can never own or
                                    # boundary a T0 segment, so it joins the
                                    # active segment as a guest instead of
                                    # waiting on a seal that cannot exist.
                                    # Otherwise, only a proven terminal owner
                                    # without a canonical boundary lane is
                                    # sealed; a proven terminal nonboundary
                                    # incoming run joins as a guest when that
                                    # seal is refused.
                                    if await _incoming_transcript_run_is_orphan(
                                        db,
                                        transcript_event=transcript_event,
                                        boundary_error=boundary_error,
                                    ):
                                        t0_result = _append_t0_event(join_active_segment=True)
                                    elif await _seal_terminal_boundary_pending_owner(
                                        db,
                                        transcript_event=transcript_event,
                                        boundary_error=boundary_error,
                                    ):
                                        t0_result = _append_t0_event()
                                    elif await _incoming_transcript_run_is_terminal_nonboundary(
                                        db,
                                        transcript_event=transcript_event,
                                        boundary_error=boundary_error,
                                    ):
                                        t0_result = _append_t0_event(join_active_segment=True)
                                    else:
                                        raise
                                segment_id = t0_result.segment_id
                                t0_event_id = t0_result.event_id
                                t0_sequence = t0_result.sequence
                            else:
                                segment_id = existing_t0_event.segment_id
                                t0_event_id = existing_t0_event.event_id
                                t0_sequence = existing_t0_event.sequence
                        except Exception as exc:  # noqa: BLE001 - persist retryable projection failure.
                            projection_error = f"{type(exc).__name__}: {exc}"
                            transcript_event.projection_status = "failed"
                            transcript_event.projection_error = projection_error
                            metadata.update(
                                {
                                    "t0_bridge_pending": True,
                                    "t0_bridge_last_error": projection_error,
                                    "t0_bridge_attempts": transcript_event.projection_attempts,
                                }
                            )
                            transcript_event.metadata_json = metadata
                            _STATE["last_error"] = projection_error
                            projection_failed = True
                        else:
                            metadata.update(
                                {
                                    "t0_bridge_pending": False,
                                    "t0_bridge_relayed_at": _now_iso(),
                                    "t0_bridge_relay_source": "runtime_control_bus",
                                    "t0_bridge_segment_id": segment_id,
                                    "t0_bridge_event_id": t0_event_id,
                                    "t0_bridge_sequence": t0_sequence,
                                }
                            )
                            transcript_event.metadata_json = metadata
                            transcript_event.projection_status = "projected"
                            transcript_event.projection_error = None
                            transcript_event.projected_at = datetime.now(timezone.utc)
                            return True

        if projection_failed and attempt >= attempts - 1:
            return False

        if predecessor_event_ids:
            # One bridge call must drain the whole committed unfinished
            # prefix, not one row per retry attempt: a canonical terminal
            # boundary waits on this drain, and its outbox retry envelope is
            # far shorter than a one-row-at-a-time catch-up. Each predecessor
            # is bridged as the session's earliest unfinished row in
            # transcript order, and the first failure stops the drain so an
            # exhausted or failing frontier is never bypassed.
            drained = True
            for prior_event_id in predecessor_event_ids:
                if not await bridge_transcript_event_to_t0(
                    transcript_event_id=prior_event_id,
                    attempts=1,
                    retry_delay_seconds=retry_delay_seconds,
                ):
                    drained = False
                    break
            if drained:
                continue

        if attempt < attempts - 1:
            await asyncio.sleep(retry_delay_seconds)

    _STATE["last_error"] = f"LookupError: transcript_event {event_uuid} not visible after {attempts} attempts"
    return False


async def sweep_pending_transcript_t0_bridges(*, limit: int = 100, max_attempts: int = 20) -> int:
    """Recover committed transcript projections after publish/process failure.

    Only a session's frontier row — the earliest unfinished one — is selected.
    Bridging a later row drains its unfinished predecessors first, so
    selecting a descendant would re-drive an earlier failed row (even one
    already past its attempt cap) through recursion and burn attempts. The
    frontier guard keeps the sweeper one ordered entry point per session;
    explicit/manual bridges keep their existing predecessor-drain behavior.
    """
    from sqlalchemy.orm import aliased

    from app.database import async_session, enter_rls_bypass
    from app.models.chat_transcript_event import ChatTranscriptEvent

    earlier_row = aliased(ChatTranscriptEvent)
    no_earlier_unfinished = ~(
        select(earlier_row.id)
        .where(
            earlier_row.session_id == ChatTranscriptEvent.session_id,
            earlier_row.sequence < ChatTranscriptEvent.sequence,
            earlier_row.projection_status.in_(("pending", "projecting", "failed")),
        )
        .exists()
    )
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="sweep pending transcript T0 projections") as bypass_db:
            result = await bypass_db.execute(
                select(ChatTranscriptEvent.id)
                .where(
                    ChatTranscriptEvent.projection_status.in_(("pending", "failed")),
                    ChatTranscriptEvent.projection_attempts < max(1, int(max_attempts)),
                    no_earlier_unfinished,
                )
                .order_by(ChatTranscriptEvent.sequence.asc())
                .limit(max(1, int(limit)))
            )
            event_ids = list(result.scalars().all())

    projected = 0
    for event_id in event_ids:
        if await bridge_transcript_event_to_t0(transcript_event_id=event_id, attempts=1):
            projected += 1
    return projected


async def _run_transcript_projection_sweeper(
    *,
    interval_seconds: float = TRANSCRIPT_PROJECTION_SWEEP_SECONDS,
) -> None:
    while True:
        try:
            await sweep_pending_transcript_t0_bridges()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the next sweep remains a recovery path.
            _STATE["last_error"] = f"projection_sweeper:{type(exc).__name__}:{exc}"
            logger.warning("[RuntimeControlBus] transcript projection sweep failed: {}", exc)
        await asyncio.sleep(max(0.1, interval_seconds))


async def handle_runtime_control_message(message: dict[str, Any]) -> bool:
    if message.get("schema") != RUNTIME_CONTROL_SCHEMA:
        return False
    event_type = str(message.get("type") or "")
    _STATE["received"] = int(_STATE.get("received") or 0) + 1
    _STATE["last_type"] = event_type

    if event_type == "web_chat_cancel":
        from app.services.web_chat_runtime import apply_remote_web_chat_cancel

        return bool(await _maybe_await(apply_remote_web_chat_cancel(str(message.get("run_id") or ""))))

    if event_type == "business_task_cancel":
        from app.services.task_executor import apply_remote_business_task_cancel

        return bool(apply_remote_business_task_cancel(str(message.get("runtime_task_id") or "")))

    if event_type == "delegation_cancel":
        from app.agents.orchestrator import apply_remote_async_delegation_cancel

        return bool(await _maybe_await(apply_remote_async_delegation_cancel(str(message.get("task_id") or ""))))

    if event_type == "subagent_cancel":
        from app.services.subagent_run_service import apply_remote_subagent_cancel

        return bool(await _maybe_await(apply_remote_subagent_cancel(str(message.get("run_id") or ""))))

    if event_type == "transcript_t0_bridge":
        return bool(
            await bridge_transcript_event_to_t0(transcript_event_id=str(message.get("transcript_event_id") or ""))
        )

    if event_type == "session_lifecycle_hook":
        hook_event = HookEvent(str(message.get("event") or ""))
        messages = message.get("messages")
        if not isinstance(messages, list):
            messages = await _maybe_await(
                _load_session_lifecycle_messages(agent_id=message.get("agent_id"), session_id=message.get("session_id"))
            )
        await emit_hook(
            hook_event,
            evidence_mode="independent",
            agent_id=message.get("agent_id"),
            session_id=message.get("session_id"),
            messages=messages,
            source=str(message.get("source") or "runtime_control"),
            metadata=message.get("metadata") if isinstance(message.get("metadata"), dict) else {},
        )
        return True

    return False


async def _listen_runtime_control_once() -> None:
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(RUNTIME_CONTROL_CHANNEL)
    async for raw_message in pubsub.listen():
        if raw_message.get("type") != "message":
            continue
        try:
            raw = raw_message.get("data")
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
            await handle_runtime_control_message(payload)
        except Exception as exc:  # noqa: BLE001 - one malformed control event must not stop listener.
            _STATE["last_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("[RuntimeControlBus] failed to handle control message: {}", exc)


async def start_runtime_control_listener(
    *,
    reconnect_delay_seconds: float = RUNTIME_CONTROL_RECONNECT_SECONDS,
) -> None:
    _STATE.update({"running": True, "last_error": None})
    projection_sweeper = asyncio.create_task(
        _run_transcript_projection_sweeper(),
        name="transcript-t0-projection-sweeper",
    )
    try:
        while True:
            try:
                await _listen_runtime_control_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - process can still run; reconnect keeps control plane live.
                _STATE["last_error"] = f"{type(exc).__name__}: {exc}"
                _STATE["restart_count"] = int(_STATE.get("restart_count") or 0) + 1
                _STATE["last_restart_at"] = _now_iso()
                logger.warning("[RuntimeControlBus] listener reconnecting after error: {}", exc)
                await asyncio.sleep(max(0.0, reconnect_delay_seconds))
                continue
            _STATE["last_error"] = "RuntimeControlListenerEnded: pubsub listener returned"
            _STATE["restart_count"] = int(_STATE.get("restart_count") or 0) + 1
            _STATE["last_restart_at"] = _now_iso()
            logger.warning("[RuntimeControlBus] listener returned; reconnecting")
            await asyncio.sleep(max(0.0, reconnect_delay_seconds))
    except asyncio.CancelledError:
        raise
    finally:
        _STATE["running"] = False
        projection_sweeper.cancel()
        try:
            await projection_sweeper
        except asyncio.CancelledError:
            pass


def runtime_control_bus_snapshot() -> dict[str, Any]:
    return dict(_STATE)
