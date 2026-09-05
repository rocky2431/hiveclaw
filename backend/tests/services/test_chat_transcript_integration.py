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
