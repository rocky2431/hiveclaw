from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_concurrent_transcript_appends_allocate_unique_session_sequences(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event, read_transcript_revision

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Transcript Tenant", slug=f"transcript-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"transcript-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@transcript.test",
                password_hash="x",
                display_name="Transcript Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Transcript Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        await db.commit()

    async def append(content: str) -> int:
        async with owner_sessionmaker() as db:
            result = await append_session_event(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                actor_type="system",
                event_type="run_boundary",
                content=content,
                materialize_chat_message=False,
                bridge_to_t0=False,
            )
            await db.commit()
            return result.sequence

    sequences = await asyncio.gather(append("first"), append("second"))
    async with owner_sessionmaker() as db:
        revision = await read_transcript_revision(db, session_id=session_id, lock=True)
    assert len(set(sequences)) == 2
    assert max(sequences) - min(sequences) == 1
    assert revision == max(sequences)


async def test_committed_transcript_projects_to_t0_once_and_persists_watermark(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    import app.database as database
    import app.services.runtime_control_bus as runtime_control_bus
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Projection Tenant", slug=f"projection-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"projection-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@projection.test",
                password_hash="x",
                display_name="Projection Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Projection Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        await db.commit()

    async def no_publish(**_kwargs) -> None:
        return None

    monkeypatch.setattr(runtime_control_bus, "publish_transcript_t0_bridge", no_publish)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(
        "app.memory.t0.ledger.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    async with owner_sessionmaker() as db:
        first = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="user",
            event_type="user_message",
            role="user",
            t0_role="user",
            user_id=user_id,
            content="first committed event",
            materialize_chat_message=False,
        )
        second = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            t0_role="assistant",
            user_id=user_id,
            content="second committed event",
            materialize_chat_message=False,
        )
        await db.commit()

    assert replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path) == []
    assert await runtime_control_bus.bridge_transcript_event_to_t0(transcript_event_id=second.event_id, attempts=2)
    assert await runtime_control_bus.bridge_transcript_event_to_t0(transcript_event_id=second.event_id, attempts=1)

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.content) for event in events] == [
        ("user_message", "first committed event"),
        ("assistant_message", "second committed event"),
    ]
    async with owner_sessionmaker() as db:
        rows = [
            await db.get(ChatTranscriptEvent, first.event_id),
            await db.get(ChatTranscriptEvent, second.event_id),
        ]
        assert all(row is not None for row in rows)
        assert all(row.projection_status == "projected" for row in rows)
        assert all(row.projection_attempts == 1 for row in rows)
        assert all(row.projected_at is not None for row in rows)
        assert [row.metadata_json["t0_bridge_event_id"] for row in rows] == [event.event_id for event in events]


async def test_transcript_bridge_is_published_only_after_the_event_is_committed(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    import app.services.runtime_control_bus as runtime_control_bus
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Post Commit Tenant", slug=f"post-commit-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"post-commit-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@post-commit.test",
                password_hash="x",
                display_name="Post Commit Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Post Commit Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        await db.commit()

    published = asyncio.Event()
    visible_when_published: list[bool] = []

    async def observe_publish(*, transcript_event_id, **_kwargs) -> None:
        async with owner_sessionmaker() as observer:
            row = await observer.get(ChatTranscriptEvent, transcript_event_id)
            visible_when_published.append(row is not None)
        published.set()

    monkeypatch.setattr(runtime_control_bus, "publish_transcript_t0_bridge", observe_publish)

    async with owner_sessionmaker() as db:
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            actor_type="system",
            event_type="run_boundary",
            content="committed boundary",
            materialize_chat_message=False,
        )
        await asyncio.sleep(0)
        assert published.is_set() is False
        await db.commit()

    await asyncio.wait_for(published.wait(), timeout=1.0)
    assert visible_when_published == [True]


async def test_postgres_text_contract_repairs_nul_in_runtime_and_transcript_payloads(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="NUL Contract Tenant", slug=f"nul-contract-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"nul-contract-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@nul-contract.test",
                password_hash="x",
                display_name="NUL Contract Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="NUL Contract Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        runtime_task = RuntimeTask(
            id=run_id,
            tenant_id=tenant_id,
            task_type="web_chat_turn",
            status="running",
            parent_agent_id=agent_id,
            child_agent_id=agent_id,
            parent_session_id=str(session_id),
            child_session_id=str(session_id),
            prompt="prompt\x00tail",
            metadata_json={"nested": {"provider_payload": "meta\x00tail"}},
        )
        db.add(runtime_task)
        await db.flush()

        appended = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            user_id=user_id,
            content="answer\x00tail",
            thinking="thinking\x00tail",
            parts=[{"type": "text", "text": "part\x00tail"}],
            metadata={"nested": {"tool_result": "result\x00tail"}},
            bridge_to_t0=False,
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        stored_task = await db.get(RuntimeTask, run_id)
        stored_event = await db.get(ChatTranscriptEvent, appended.event_id)
        stored_message = await db.get(ChatMessage, appended.message_id)

        assert stored_task.prompt == r"prompt\u0000tail"
        assert stored_task.metadata_json["nested"]["provider_payload"] == r"meta\u0000tail"
        assert stored_event.content == r"answer\u0000tail"
        assert stored_event.parts_json[0]["text"] == r"part\u0000tail"
        assert stored_event.metadata_json["nested"]["tool_result"] == r"result\u0000tail"
        assert stored_message.content == r"answer\u0000tail"
        assert stored_message.thinking == r"thinking\u0000tail"


async def _prepare_terminal_owner_recovery_session(
    owner_sessionmaker,
    tmp_path,
    *,
    owner_status: str,
    owner_completed_at: datetime | None,
    owner_task_type: str = "subagent",
    owner_tenant_mismatch: bool = False,
    owner_agent_mismatch: bool = False,
    owner_session_mismatch: bool = False,
) -> SimpleNamespace:
    """Build the production incident shape: one terminal non-boundary owner.

    A plain ``subagent`` RuntimeTask (no canonical terminal-boundary lane)
    left an open T0 segment in the session — modeled by appending directly to
    the T0 ledger, because the incident's stale segment predates any repair.
    A later ``web_chat_turn`` then appends its own fenced transcript event for
    the same session. ``owner_*_mismatch`` flags point the owner row at a
    different tenant/agent/session so the recovery facts cannot be proven.
    """

    from app.memory.t0.ledger import append_t0_session_event
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import COMPLETION_OUTBOX_TERMINAL_STATUSES, RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event

    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    other_session_id = uuid.uuid4()
    owner_run_id = uuid.uuid4()
    incoming_run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        for row_tenant_id in (tenant_id, other_tenant_id):
            db.add(
                Tenant(
                    id=row_tenant_id,
                    name=f"Owner Recovery {row_tenant_id.hex[:8]}",
                    slug=f"owner-recovery-{row_tenant_id.hex[:8]}",
                )
            )
        db.add(
            User(
                id=user_id,
                username=f"owner-recovery-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@owner-recovery.test",
                password_hash="x",
                display_name="Owner Recovery Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Owner Recovery Agent", creator_id=user_id))
        db.add(Agent(id=other_agent_id, tenant_id=tenant_id, name="Owner Recovery Other Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        db.add(ChatSession(id=other_session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        owner_session_ref = str(other_session_id if owner_session_mismatch else session_id)
        db.add(
            RuntimeTask(
                id=owner_run_id,
                tenant_id=other_tenant_id if owner_tenant_mismatch else tenant_id,
                task_type=owner_task_type,
                status=owner_status,
                parent_agent_id=other_agent_id if owner_agent_mismatch else agent_id,
                child_agent_id=None,
                parent_session_id=owner_session_ref,
                child_session_id=owner_session_ref,
                completed_at=owner_completed_at,
                completion_outbox_settled_at=(
                    datetime.now(timezone.utc) if owner_status in COMPLETION_OUTBOX_TERMINAL_STATUSES else None
                ),
                prompt="terminal non-boundary owner recovery fixture",
            )
        )
        db.add(
            RuntimeTask(
                id=incoming_run_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                status="running",
                parent_agent_id=agent_id,
                child_agent_id=None,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                prompt="terminal non-boundary owner recovery incoming run",
            )
        )
        await db.flush()
        incoming_event = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=incoming_run_id,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            t0_role="assistant",
            user_id=user_id,
            content="incoming task event",
            materialize_chat_message=False,
        )
        await db.commit()

    owner_append = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="assistant_message",
        role="assistant",
        content="owner terminal event",
        tenant_id=tenant_id,
        runtime_task_id=owner_run_id,
        data_root=tmp_path,
    )

    return SimpleNamespace(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        owner_run_id=owner_run_id,
        incoming_run_id=incoming_run_id,
        owner_segment_id=owner_append.segment_id,
        incoming_event=incoming_event,
    )


def _patch_owner_recovery_lane(monkeypatch, owner_sessionmaker, tmp_path) -> None:
    import app.database as database
    import app.services.runtime_control_bus as runtime_control_bus

    async def no_publish(**_kwargs) -> None:
        return None

    monkeypatch.setattr(runtime_control_bus, "publish_transcript_t0_bridge", no_publish)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(
        "app.memory.t0.ledger.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )


async def test_terminal_nonboundary_owner_seals_stale_segment_and_projects_incoming_once(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    """A terminal subagent owner with no boundary lane must not strand the session.

    Reproduces the production facts: the open T0 segment's owner is a terminal
    ``subagent`` RuntimeTask with ``completed_at`` and NULL
    ``terminal_boundary_generation``. The incoming event first fails the owner
    mismatch, the shared mismatch boundary proves every mechanical fact, seals
    exactly that owner's segment with a deterministic boundary identity, and
    the retried append projects the incoming event exactly once.
    """

    import app.services.runtime_control_bus as runtime_control_bus
    from app.memory.t0.ledger import replay_t0_session_events, seal_t0_session_segment
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask

    _patch_owner_recovery_lane(monkeypatch, owner_sessionmaker, tmp_path)
    fx = await _prepare_terminal_owner_recovery_session(
        owner_sessionmaker,
        tmp_path,
        owner_status="needs_reconciliation",
        owner_completed_at=datetime.now(timezone.utc),
    )

    # The incoming append fails the owner mismatch, the recovery seals the
    # terminal owner's segment, and the single retry projects the event.
    assert await runtime_control_bus.bridge_transcript_event_to_t0(
        transcript_event_id=fx.incoming_event.event_id, attempts=1
    )

    async with owner_sessionmaker() as db:
        incoming_row = await db.get(ChatTranscriptEvent, fx.incoming_event.event_id)
        assert incoming_row.projection_status == "projected"
        assert incoming_row.projection_error is None
        assert incoming_row.projection_attempts == 1
        assert incoming_row.metadata_json["t0_bridge_segment_id"] != fx.owner_segment_id
        owner_task = await db.get(RuntimeTask, fx.owner_run_id)
        assert owner_task.status == "needs_reconciliation"
        assert owner_task.terminal_boundary_generation is None

    events = replay_t0_session_events(agent_id=fx.agent_id, session_id=fx.session_id, data_root=tmp_path)
    assert [(event.event_type, event.content) for event in events] == [
        ("assistant_message", "owner terminal event"),
        ("segment_boundary", "terminal_owner_segment_recovery"),
        ("assistant_message", "incoming task event"),
    ]
    boundary_event = events[1]
    assert boundary_event.runtime_task_id == fx.owner_run_id.hex
    assert boundary_event.metadata["recovery"] == "terminal_nonboundary_owner"
    assert boundary_event.metadata["owner_task_type"] == "subagent"
    assert boundary_event.metadata["owner_status"] == "needs_reconciliation"
    assert boundary_event.metadata["incoming_runtime_task_id"] == fx.incoming_run_id.hex
    assert boundary_event.metadata["boundary_idempotency_key_sha256"]
    assert [event for event in events if event.metadata.get("transcript_event_id") == str(fx.incoming_event.event_id)]

    # Deterministic replay: the same recovery boundary identity must replay
    # the original receipt instead of sealing the incoming task's open segment,
    # and re-bridging the projected row must not duplicate any T0 event.
    replayed_seal = seal_t0_session_segment(
        agent_id=fx.agent_id,
        session_id=fx.session_id,
        reason="terminal_owner_segment_recovery",
        idempotency_key=(f"{runtime_control_bus.TERMINAL_OWNER_SEGMENT_RECOVERY_IDEMPOTENCY_KEY}:{fx.owner_run_id}"),
        expected_runtime_task_id=fx.owner_run_id,
        data_root=tmp_path,
    )
    assert replayed_seal is not None
    assert replayed_seal.segment_id == fx.owner_segment_id
    assert replayed_seal.event_id == boundary_event.event_id
    assert await runtime_control_bus.bridge_transcript_event_to_t0(
        transcript_event_id=fx.incoming_event.event_id, attempts=1
    )

    replayed = replay_t0_session_events(agent_id=fx.agent_id, session_id=fx.session_id, data_root=tmp_path)
    assert [event.event_id for event in replayed] == [event.event_id for event in events]
    assert [event.event_type for event in replayed].count("segment_boundary") == 1


async def test_boundary_pending_owner_recovery_fails_closed_without_proven_owner_facts(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    """Incomplete, canonical-boundary, and mismatched owners must never auto-seal.

    A live owner may still append; a terminal owner with a canonical
    ``terminal_boundary_generation`` has a real outbox that owns its boundary
    identity; and a tenant/agent/session mismatch is not this transcript
    event's owner. Each case fails exactly as before the recovery existed.
    """

    import app.services.runtime_control_bus as runtime_control_bus
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask

    _patch_owner_recovery_lane(monkeypatch, owner_sessionmaker, tmp_path)
    scenarios = {
        "running_owner": dict(owner_status="running", owner_completed_at=None),
        "terminal_without_completed_at": dict(owner_status="completed", owner_completed_at=None),
        "canonical_boundary_owner": dict(
            owner_status="completed",
            owner_completed_at=datetime.now(timezone.utc),
            owner_task_type="web_chat_turn",
        ),
        "tenant_mismatch": dict(
            owner_status="completed",
            owner_completed_at=datetime.now(timezone.utc),
            owner_tenant_mismatch=True,
        ),
        "agent_mismatch": dict(
            owner_status="completed",
            owner_completed_at=datetime.now(timezone.utc),
            owner_agent_mismatch=True,
        ),
        "session_mismatch": dict(
            owner_status="completed",
            owner_completed_at=datetime.now(timezone.utc),
            owner_session_mismatch=True,
        ),
    }
    for scenario, kwargs in scenarios.items():
        fx = await _prepare_terminal_owner_recovery_session(owner_sessionmaker, tmp_path, **kwargs)
        assert (
            await runtime_control_bus.bridge_transcript_event_to_t0(
                transcript_event_id=fx.incoming_event.event_id, attempts=1
            )
            is False
        ), scenario

        async with owner_sessionmaker() as db:
            row = await db.get(ChatTranscriptEvent, fx.incoming_event.event_id)
            assert row.projection_status == "failed", scenario
            assert row.projection_error.startswith("T0SegmentBoundaryPending:"), scenario
            assert row.projection_attempts == 1, scenario
            owner_task = await db.get(RuntimeTask, fx.owner_run_id)
            assert owner_task.status == kwargs["owner_status"], scenario

        events = replay_t0_session_events(agent_id=fx.agent_id, session_id=fx.session_id, data_root=tmp_path)
        assert [event.event_type for event in events] == ["assistant_message"], scenario
        assert events[0].content == "owner terminal event", scenario


async def _prepare_boundary_mismatch_session(
    owner_sessionmaker,
    tmp_path,
    *,
    incoming_task_row: bool,
) -> SimpleNamespace:
    """One open T0 segment owned by a live canonical task plus a mismatching row.

    The incoming run identity is metadata-carried when the incoming run has no
    RuntimeTask row — the only orphan shape the ``run_id`` FK permits (the
    column stays NULL while ``metadata_json.runtime_task_id`` keeps the deleted
    run's id) — and column-carried when a real mismatching RuntimeTask row
    exists.
    """

    from app.memory.t0.ledger import append_t0_session_event
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    owner_run_id = uuid.uuid4()
    incoming_run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            Tenant(id=tenant_id, name=f"Orphan Bridge {tenant_id.hex[:8]}", slug=f"orphan-bridge-{tenant_id.hex[:8]}")
        )
        db.add(
            User(
                id=user_id,
                username=f"orphan-bridge-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@orphan-bridge.test",
                password_hash="x",
                display_name="Orphan Bridge Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Orphan Bridge Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        db.add(
            RuntimeTask(
                id=owner_run_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                status="running",
                parent_agent_id=agent_id,
                child_agent_id=None,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                prompt="orphan bridge owner run",
            )
        )
        if incoming_task_row:
            db.add(
                RuntimeTask(
                    id=incoming_run_id,
                    tenant_id=tenant_id,
                    task_type="subagent",
                    status="running",
                    parent_agent_id=agent_id,
                    child_agent_id=None,
                    parent_session_id=str(session_id),
                    child_session_id=str(session_id),
                    prompt="orphan bridge mismatching live run",
                )
            )
        await db.flush()
        incoming_metadata = {"turn_id": "turn-incoming-1"}
        if not incoming_task_row:
            incoming_metadata["runtime_task_id"] = str(incoming_run_id)
        incoming_event = await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=incoming_run_id if incoming_task_row else None,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            t0_role="assistant",
            user_id=user_id,
            content="incoming mismatching run event",
            metadata=incoming_metadata,
            materialize_chat_message=False,
        )
        await db.commit()

    owner_append = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="assistant_message",
        role="assistant",
        content="owner open segment event",
        tenant_id=tenant_id,
        runtime_task_id=owner_run_id,
        data_root=tmp_path,
    )

    return SimpleNamespace(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        user_id=user_id,
        owner_run_id=owner_run_id,
        incoming_run_id=incoming_run_id,
        owner_segment_id=owner_append.segment_id,
        incoming_event=incoming_event,
    )


async def test_orphan_incoming_run_joins_active_segment_as_guest_without_sealing(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    """A metadata-only run id with no RuntimeTask row must not wait on a seal.

    A deleted run leaves its identity only in transcript metadata (the run_id
    column is NULL under the FK). Such a run can never own or boundary a T0
    segment, so the bridge projects it as a guest event of the session's
    already-open segment: the canonical owner keeps the segment, no boundary is
    sealed, and the original run/turn ids survive only as bridge-owned
    provenance keys.
    """

    import json as jsonlib

    import app.services.runtime_control_bus as runtime_control_bus
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask

    _patch_owner_recovery_lane(monkeypatch, owner_sessionmaker, tmp_path)
    fx = await _prepare_boundary_mismatch_session(owner_sessionmaker, tmp_path, incoming_task_row=False)

    assert await runtime_control_bus.bridge_transcript_event_to_t0(
        transcript_event_id=fx.incoming_event.event_id, attempts=1
    )

    async with owner_sessionmaker() as db:
        row = await db.get(ChatTranscriptEvent, fx.incoming_event.event_id)
        assert row.projection_status == "projected"
        assert row.projection_error is None
        assert row.projection_attempts == 1
        # The committed transcript truth keeps its original identity metadata.
        assert row.metadata_json["runtime_task_id"] == str(fx.incoming_run_id)
        assert row.metadata_json["turn_id"] == "turn-incoming-1"
        owner_task = await db.get(RuntimeTask, fx.owner_run_id)
        assert owner_task.status == "running"
        # The canonical owner keeps its boundary lane untouched: the guest
        # join neither enqueued nor consumed the owner's terminal boundary.
        assert owner_task.terminal_boundary_generation == 1
        assert owner_task.terminal_boundary_enqueued_at is None

    events = replay_t0_session_events(agent_id=fx.agent_id, session_id=fx.session_id, data_root=tmp_path)
    assert [(event.event_type, event.segment_id) for event in events] == [
        ("assistant_message", fx.owner_segment_id),
        ("assistant_message", fx.owner_segment_id),
    ]
    owner_event, guest_event = events
    assert owner_event.runtime_task_id == fx.owner_run_id.hex
    assert guest_event.runtime_task_id is None
    assert guest_event.turn_id is None
    assert guest_event.metadata["t0_bridge_joined_segment_run_id"] == str(fx.incoming_run_id)
    assert guest_event.metadata["t0_bridge_joined_segment_turn_id"] == "turn-incoming-1"
    assert "runtime_task_id" not in guest_event.metadata
    assert "turn_id" not in guest_event.metadata

    # The canonical owner still owns the open segment — no seal happened.
    index_path = tmp_path / str(fx.agent_id) / "memory" / "t0" / "sessions" / str(fx.session_id) / "index.json"
    index = jsonlib.loads(index_path.read_text(encoding="utf-8"))
    assert index["active_segment_id"] == fx.owner_segment_id
    segment = next(s for s in index["segments"] if s["segment_id"] == fx.owner_segment_id)
    assert segment["state"] == "open"
    assert segment["runtime_task_id"] == fx.owner_run_id.hex


async def test_live_mismatching_runtime_task_still_fails_closed(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    """A real tenant-matching RuntimeTask row must never be guest-joined.

    The orphan recovery only covers runs with no tenant-matching RuntimeTask
    row. A live incoming task may still append and enqueue its own canonical
    boundary later, so guest-joining it would corrupt the owner's turn; the
    owner mismatch fails closed exactly as before the orphan branch existed.
    """

    import app.services.runtime_control_bus as runtime_control_bus
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask

    _patch_owner_recovery_lane(monkeypatch, owner_sessionmaker, tmp_path)
    fx = await _prepare_boundary_mismatch_session(owner_sessionmaker, tmp_path, incoming_task_row=True)

    assert (
        await runtime_control_bus.bridge_transcript_event_to_t0(
            transcript_event_id=fx.incoming_event.event_id, attempts=1
        )
        is False
    )

    async with owner_sessionmaker() as db:
        row = await db.get(ChatTranscriptEvent, fx.incoming_event.event_id)
        assert row.projection_status == "failed"
        assert row.projection_error.startswith("T0SegmentBoundaryPending:")
        assert row.projection_attempts == 1
        assert "t0_bridge_joined_segment_run_id" not in row.metadata_json
        assert "t0_bridge_joined_segment_turn_id" not in row.metadata_json
        owner_task = await db.get(RuntimeTask, fx.owner_run_id)
        assert owner_task.status == "running"

    events = replay_t0_session_events(agent_id=fx.agent_id, session_id=fx.session_id, data_root=tmp_path)
    assert [(event.event_type, event.runtime_task_id) for event in events] == [
        ("assistant_message", fx.owner_run_id.hex)
    ]


async def test_sweeper_selects_only_each_sessions_earliest_unfinished_frontier(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
) -> None:
    """The sweeper must not re-drive a capped frontier through its descendants.

    Selecting a later pending row would drain its unfinished predecessors
    recursively and burn attempts on an earlier failed row already past its
    cap. Only each session's earliest unfinished row is an entry point, so a
    capped frontier parks that session while other sessions' frontiers still
    recover.
    """

    import app.services.runtime_control_bus as runtime_control_bus
    from app.memory.t0.ledger import append_t0_session_event, replay_t0_session_events
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event

    _patch_owner_recovery_lane(monkeypatch, owner_sessionmaker, tmp_path)

    # The sweeper selects unfinished rows globally by design (RLS bypass), and
    # the PG container is shared across the whole test session. Earlier tests'
    # finished-with residue must not change this test's selection, so park any
    # pre-existing unfinished row before building this test's own sessions.
    from sqlalchemy import update

    async with owner_sessionmaker() as db:
        await db.execute(
            update(ChatTranscriptEvent)
            .where(ChatTranscriptEvent.projection_status.in_(("pending", "projecting", "failed")))
            .values(projection_status="projected")
        )
        await db.commit()

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    stalled_session_id = uuid.uuid4()
    recoverable_session_id = uuid.uuid4()
    owner_run_id = uuid.uuid4()
    capped_run_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            Tenant(
                id=tenant_id, name=f"Sweeper Frontier {tenant_id.hex[:8]}", slug=f"sweeper-frontier-{tenant_id.hex[:8]}"
            )
        )
        db.add(
            User(
                id=user_id,
                username=f"sweeper-frontier-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@sweeper-frontier.test",
                password_hash="x",
                display_name="Sweeper Frontier Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Sweeper Frontier Agent", creator_id=user_id))
        await db.flush()
        for session in (stalled_session_id, recoverable_session_id):
            db.add(ChatSession(id=session, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        for run_id, task_type in ((owner_run_id, "web_chat_turn"), (capped_run_id, "subagent")):
            db.add(
                RuntimeTask(
                    id=run_id,
                    tenant_id=tenant_id,
                    task_type=task_type,
                    status="running",
                    parent_agent_id=agent_id,
                    child_agent_id=None,
                    parent_session_id=str(stalled_session_id),
                    child_session_id=str(stalled_session_id),
                    prompt="sweeper frontier fixture run",
                )
            )
        await db.flush()

        async def _append(session_id, *, content, run_id=None):
            return await append_session_event(
                db=db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                run_id=run_id,
                actor_type="assistant",
                event_type="assistant_message",
                role="assistant",
                t0_role="assistant",
                user_id=user_id,
                content=content,
                materialize_chat_message=False,
            )

        capped_event = await _append(stalled_session_id, content="capped frontier event", run_id=capped_run_id)
        later_descendant = await _append(stalled_session_id, content="later pending descendant")
        recoverable_frontier = await _append(recoverable_session_id, content="recoverable frontier event")
        later_sibling = await _append(recoverable_session_id, content="later pending sibling")
        await db.commit()

    # The stalled session's segment stays owned by a live task, so the capped
    # run's mismatch fails closed; the mechanical attempts column is then set
    # to the sweeper cap to model an exhausted attempt history.
    append_t0_session_event(
        agent_id=agent_id,
        session_id=stalled_session_id,
        event_type="assistant_message",
        role="assistant",
        content="stalled session owner event",
        tenant_id=tenant_id,
        runtime_task_id=owner_run_id,
        data_root=tmp_path,
    )
    assert (
        await runtime_control_bus.bridge_transcript_event_to_t0(transcript_event_id=capped_event.event_id, attempts=1)
        is False
    )
    async with owner_sessionmaker() as db:
        capped_row = await db.get(ChatTranscriptEvent, capped_event.event_id)
        assert capped_row.projection_status == "failed"
        capped_row.projection_attempts = 20
        await db.commit()

    assert await runtime_control_bus.sweep_pending_transcript_t0_bridges(limit=50, max_attempts=20) == 1

    async with owner_sessionmaker() as db:
        rows = {
            event_id: await db.get(ChatTranscriptEvent, event_id)
            for event_id in (
                capped_event.event_id,
                later_descendant.event_id,
                recoverable_frontier.event_id,
                later_sibling.event_id,
            )
        }
        # The capped frontier parks its whole session: nothing is selected and
        # no attempt is burned on the capped row or its later descendant.
        assert rows[capped_event.event_id].projection_status == "failed"
        assert rows[capped_event.event_id].projection_attempts == 20
        assert rows[later_descendant.event_id].projection_status == "pending"
        assert rows[later_descendant.event_id].projection_attempts == 0
        # Other sessions recover exactly their own earliest unfinished row.
        assert rows[recoverable_frontier.event_id].projection_status == "projected"
        assert rows[recoverable_frontier.event_id].projection_attempts == 1
        assert rows[later_sibling.event_id].projection_status == "pending"
        assert rows[later_sibling.event_id].projection_attempts == 0

    stalled_events = replay_t0_session_events(agent_id=agent_id, session_id=stalled_session_id, data_root=tmp_path)
    assert [event.event_type for event in stalled_events] == ["assistant_message"]
    recoverable_events = replay_t0_session_events(
        agent_id=agent_id, session_id=recoverable_session_id, data_root=tmp_path
    )
    assert [event.content for event in recoverable_events] == ["recoverable frontier event"]
