"""SESSION-V2-DELETE-ORDER-001 regression: production-shaped Session deletion.

Real-PostgreSQL reproduction of the supported ``DELETE
/api/v1/chat/{agent_id}/sessions/{session_id}`` route against a populated
Session V2 session: 1,137 transcript events with self-references, a real
round-committed ``session_model_results`` row, a terminal
``session_run_outcomes`` row, an outbox row, and a large same-table filler
volume that mirrors the rest of the production transcript table.

Mechanism under test: inbound foreign keys into ``chat_transcript_events.id``
fire one referential-integrity probe per deleted row. Without supporting
indexes on the referencing columns, each probe is a sequential scan of a
large table and the single transcript DELETE exceeds the statement timeout
(production observed asyncpg QueryCanceledError after 30.080s). The test
uses a 5s per-statement timeout: the mechanism is identical, the failure
just reproduces faster.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

import app.api.chat_sessions as chat_sessions_api
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_feedback import SessionFeedbackEvent
from app.models.session_v2 import (
    SessionEventOutbox,  # noqa: F401 - registers transcript FK targets
    SessionModelResult,
    SessionRunOutcome,
    SessionToolInvocation,
)
from app.models.tenant import Tenant
from app.models.user import User

# Exact production shape of the first P01 session whose deletion timed out.
TARGET_EVENT_COUNT = 1137
# Same-table filler volume standing in for the rest of the production
# transcript table; large enough that per-row sequential scans blow the
# statement timeout.
FILLER_EVENT_COUNT = 200_000
# Production runs a 30s statement timeout; the per-row RI-scan mechanism is
# identical at 5s, so the regression reproduces in CI-sized time.
TEST_STATEMENT_TIMEOUT_MS = 5_000


async def _seed_populated_session(owner_sessionmaker) -> dict:
    suffix = uuid.uuid4().hex[:10]
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"Delete Regression Tenant {suffix}", slug=f"del-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"del-{suffix}",
                email=f"del-{suffix}@example.test",
                password_hash="x",
                display_name="Delete Regression Owner",
                tenant_id=tenant_id,
                role="member",
            )
        )
        await db.flush()
        agent = Agent(
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name=f"Delete Regression Agent {suffix}",
            role_description="Exercises the supported session delete route.",
            status="idle",
        )
        db.add(agent)
        await db.flush()

        target_session = ChatSession(
            agent_id=agent.id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=f"target-{suffix}",
        )
        filler_session = ChatSession(
            agent_id=agent.id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=f"filler-{suffix}",
        )
        db.add_all([target_session, filler_session])
        await db.flush()

        previous_event_id: uuid.UUID | None = None
        for index in range(1, TARGET_EVENT_COUNT + 1):
            event = ChatTranscriptEvent(
                sequence=index,
                tenant_id=tenant_id,
                agent_id=agent.id,
                session_id=target_session.id,
                schema_version=1,
                item_type="user_message" if index == 1 else "agent_message",
                item_status="succeeded",
                actor_type="user" if index == 1 else "assistant",
                event_type="user_message" if index == 1 else "assistant_message",
                visibility_scope="direct_user",
                listed_surface="chat",
                content=f"message-{index}",
                metadata_json={"source": "web", "role": "user" if index == 1 else "assistant"},
                projection_status="projected",
                projection_attempts=1,
                # Self-referential chain: every other event points at its
                # predecessor, exercising the un-indexed self foreign key.
                parent_event_id=previous_event_id if index % 2 == 0 else None,
            )
            db.add(event)
            await db.flush()
            previous_event_id = event.id

        session_message = ChatMessage(
            agent_id=agent.id,
            tenant_id=tenant_id,
            user_id=user_id,
            role="user",
            content="delete-regression-message",
            conversation_id=str(target_session.id),
        )
        db.add(session_message)
        await db.flush()

        run = RuntimeTask(
            tenant_id=tenant_id,
            task_type="delegation",
            parent_agent_id=agent.id,
            parent_session_id=str(target_session.id),
            root_idempotency_key=f"del-{suffix}",
            config_snapshot_hash="0" * 64,
            policy_snapshot_hash="0" * 64,
        )
        db.add(run)
        await db.flush()
        # The Session V2 authority trigger binds model results to the run's
        # derived turn id: 'turn-' || hex(run id).
        turn_id = f"turn-{run.id.hex}"
        round_id = f"round-{run.id.hex}"
        model_result = SessionModelResult(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            session_id=target_session.id,
            turn_id=turn_id,
            run_id=run.id,
            round_id=round_id,
            provider_request_id=f"provider-{suffix}",
            state="sealed",
            model_request_hash="0" * 64,
            model_request_snapshot_json={},
            bound_input_ids_json=[],
        )
        db.add(model_result)
        await db.flush()
        # Terminal event: a real schema-v2 result_commit.round_committed event
        # bound to the run, result, and round, exactly as a completed Session
        # V2 run writes it.
        commit_event = ChatTranscriptEvent(
            sequence=TARGET_EVENT_COUNT + 1,
            tenant_id=tenant_id,
            agent_id=agent.id,
            session_id=target_session.id,
            schema_version=2,
            item_id=uuid.uuid4(),
            item_kind="result_commit",
            lifecycle="round_committed",
            payload_schema="hive.session.payload.result_commit.round_committed.v2",
            scope_json={
                "level": "round",
                "session_id": str(target_session.id),
                "thread_id": str(target_session.id),
                "turn_id": turn_id,
                "run_id": str(run.id),
                "round_id": round_id,
            },
            ordinal=1,
            run_id=run.id,
            result_id=model_result.id,
            item_type="result_commit",
            item_status="succeeded",
            actor_type="runtime",
            event_type="result_commit.round_committed",
            visibility_scope="direct_user",
            listed_surface="chat",
            content=None,
            metadata_json={
                "v2_payload": {},
                "actor": {"type": "runtime"},
                "visibility": {"audience": "direct_user"},
                "source": "web",
            },
            projection_status="projected",
            projection_attempts=1,
        )
        db.add(commit_event)
        await db.flush()
        # Close the receipt loop: the result row now points at its commit event.
        model_result.round_committed_event_id = commit_event.id
        model_result.state = "round_committed"
        await db.flush()
        db.add(
            SessionRunOutcome(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                session_id=target_session.id,
                turn_id=turn_id,
                run_id=run.id,
                terminal_result_id=model_result.id,
                state="terminal_committed",
                eligibility_snapshot_hash="0" * 64,
                terminal_event_id=commit_event.id,
            )
        )
        db.add(
            SessionEventOutbox(
                tenant_id=tenant_id,
                session_id=target_session.id,
                event_id=commit_event.id,
                sequence=TARGET_EVENT_COUNT + 1,
                envelope_json={"kind": "delete-regression"},
                envelope_sha256="0" * 64,
                status="published",
            )
        )
        # Settled tool invocation: result_event_id is a populated NO ACTION
        # FK into the transcript, exactly as the active tool runtime writes
        # it after every tool settlement. The Session V2 authority trigger
        # requires a matching completed tool_result event.
        tool_invocation_id = uuid.uuid4()
        tool_use_id = f"tool-use-{suffix}"
        # Insert order mirrors the tool runtime: the invocation row is
        # created before its result event (result_event_id NULL), the
        # tool_result event then references the invocation, and finally the
        # settled invocation takes the populated NO ACTION result_event_id.
        db.add(
            SessionToolInvocation(
                id=tool_invocation_id,
                tenant_id=tenant_id,
                session_id=target_session.id,
                run_id=run.id,
                round_id=round_id,
                provider_request_id=f"provider-req-{suffix}",
                provider_tool_use_id=tool_use_id,
                invocation_item_id=uuid.uuid4(),
                args_hash="0" * 64,
                authority_snapshot_hash="0" * 64,
                effect_idempotency_key=f"effect-{suffix}",
                effect_state="effect_started",
            )
        )
        await db.flush()
        tool_result_event = ChatTranscriptEvent(
            sequence=TARGET_EVENT_COUNT + 2,
            tenant_id=tenant_id,
            agent_id=agent.id,
            session_id=target_session.id,
            schema_version=2,
            item_id=uuid.uuid4(),
            item_kind="tool_result",
            lifecycle="completed",
            payload_schema="hive.session.payload.tool_result.completed.v2",
            scope_json={
                "level": "round",
                "session_id": str(target_session.id),
                "thread_id": str(target_session.id),
                "turn_id": turn_id,
                "run_id": str(run.id),
                "round_id": round_id,
            },
            ordinal=2,
            run_id=run.id,
            invocation_id=tool_invocation_id,
            provider_tool_use_id=tool_use_id,
            item_type="tool_result",
            item_status="succeeded",
            actor_type="runtime",
            event_type="tool_result.completed",
            visibility_scope="direct_user",
            listed_surface="chat",
            content=None,
            metadata_json={
                "v2_payload": {},
                "actor": {"type": "runtime"},
                "visibility": {"audience": "direct_user"},
                "source": "web",
            },
            projection_status="projected",
            projection_attempts=1,
        )
        db.add(tool_result_event)
        await db.flush()
        await db.execute(
            text(
                "UPDATE session_tool_invocations "
                "SET result_event_id=:event_id,effect_state='effect_committed' "
                "WHERE id=:invocation_id"
            ).bindparams(event_id=tool_result_event.id, invocation_id=tool_invocation_id)
        )
        await db.flush()
        # Session-owned feedback: NOT NULL NO ACTION session_id blocks the
        # Session row delete unless the feedback goes in the same transaction.
        db.add(
            SessionFeedbackEvent(
                tenant_id=tenant_id,
                agent_id=agent.id,
                session_id=target_session.id,
                message_id=session_message.id,
                user_id=user_id,
                label="useful",
                reason="delete-regression",
            )
        )
        # Transcript artifact: the production shape ("seven artifacts")
        # referenced the route's chat_messages.session_id NO ACTION edge.
        db.add(
            ChatArtifact(
                agent_id=agent.id,
                tenant_id=tenant_id,
                session_id=target_session.id,
                message_id=session_message.id,
                runtime_task_id=run.id,
                authority_state="owned",
                path=f"artifacts/{suffix}/report.md",
                name="report.md",
                snapshot_hash="0" * 64,
            )
        )
        await db.commit()

        # Large same-table filler volume via set-based SQL (the rest of the
        # production transcript table). Raw SQL keeps the seed fast.
        await db.execute(
            text(
                "INSERT INTO chat_transcript_events "
                "(id, sequence, tenant_id, agent_id, session_id, schema_version, item_type, "
                "item_status, actor_type, event_type, visibility_scope, listed_surface, content, "
                "metadata_json, projection_status, projection_attempts) "
                "SELECT gen_random_uuid(), g, :tenant_id, :agent_id, :filler_session_id, 1, "
                "'agent_message', 'succeeded', 'assistant', 'assistant_message', 'direct_user', "
                "'chat', 'filler', '{}'::jsonb, 'projected', 1 "
                "FROM generate_series(1, :filler_count) AS g"
            ).bindparams(
                tenant_id=tenant_id,
                agent_id=agent.id,
                filler_session_id=filler_session.id,
                filler_count=FILLER_EVENT_COUNT,
            )
        )
        await db.commit()

        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "agent_id": agent.id,
            "target_session_id": target_session.id,
            "filler_session_id": filler_session.id,
            "run_id": run.id,
        }


@pytest.mark.asyncio
async def test_supported_delete_route_removes_populated_session_within_statement_timeout(
    owner_sessionmaker, monkeypatch
) -> None:
    ids = await _seed_populated_session(owner_sessionmaker)

    agent = SimpleNamespace(id=ids["agent_id"], creator_id=ids["user_id"], tenant_id=ids["tenant_id"])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, "use"

    async def fake_authorize_loaded_session(**_kwargs):
        return "session_owner"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr(chat_sessions_api, "_authorize_loaded_session", fake_authorize_loaded_session, raising=False)

    async with owner_sessionmaker() as db:
        await db.execute(text(f"SET statement_timeout = {TEST_STATEMENT_TIMEOUT_MS}"))
        started = time.monotonic()
        await chat_sessions_api.delete_session(
            agent_id=ids["agent_id"],
            session_id=ids["target_session_id"],
            current_user=SimpleNamespace(id=ids["user_id"], role="member"),
            db=db,
        )
        elapsed = time.monotonic() - started

    # The whole atomic deletion must fit well inside one normal statement
    # timeout budget, matching the production contract (30s) with margin.
    assert elapsed < TEST_STATEMENT_TIMEOUT_MS / 1000.0

    async with owner_sessionmaker() as db:
        remaining_sessions = await db.scalar(
            select(func.count()).select_from(ChatSession).where(ChatSession.id == ids["target_session_id"])
        )
        remaining_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(ChatTranscriptEvent.session_id == ids["target_session_id"])
        )
        remaining_outbox = await db.scalar(
            select(func.count())
            .select_from(SessionEventOutbox)
            .where(SessionEventOutbox.session_id == ids["target_session_id"])
        )
        remaining_results = await db.scalar(
            select(func.count())
            .select_from(SessionModelResult)
            .where(SessionModelResult.session_id == ids["target_session_id"])
        )
        remaining_outcomes = await db.scalar(
            select(func.count())
            .select_from(SessionRunOutcome)
            .where(SessionRunOutcome.session_id == ids["target_session_id"])
        )
        remaining_messages = await db.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.conversation_id == str(ids["target_session_id"]))
        )
        remaining_tool_invocations = await db.scalar(
            select(func.count())
            .select_from(SessionToolInvocation)
            .where(SessionToolInvocation.session_id == ids["target_session_id"])
        )
        remaining_feedback = await db.scalar(
            select(func.count())
            .select_from(SessionFeedbackEvent)
            .where(SessionFeedbackEvent.session_id == ids["target_session_id"])
        )
        remaining_artifacts = await db.scalar(
            select(func.count()).select_from(ChatArtifact).where(ChatArtifact.session_id == ids["target_session_id"])
        )
        # RuntimeTask and its terminal evidence are contractually outside
        # Session deletion (runtime_tasks.parent_session_id is text, not an
        # FK); the run must survive the session delete.
        surviving_runs = await db.scalar(
            select(func.count()).select_from(RuntimeTask).where(RuntimeTask.id == ids["run_id"])
        )
        # Unrelated sessions are untouched: the filler session keeps every row.
        filler_sessions = await db.scalar(
            select(func.count()).select_from(ChatSession).where(ChatSession.id == ids["filler_session_id"])
        )
        filler_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(ChatTranscriptEvent.session_id == ids["filler_session_id"])
        )

    assert remaining_sessions == 0
    assert remaining_events == 0
    assert remaining_outbox == 0
    assert remaining_results == 0
    assert remaining_outcomes == 0
    assert remaining_messages == 0
    assert remaining_tool_invocations == 0
    assert remaining_feedback == 0
    assert remaining_artifacts == 0
    assert surviving_runs == 1
    assert filler_sessions == 1
    assert filler_events == FILLER_EVENT_COUNT

    # Leave the shared container clean: drop the 200k-row filler session.
    async with owner_sessionmaker() as db:
        await db.execute(
            text("DELETE FROM chat_transcript_events WHERE session_id = :filler_session_id"),
            {"filler_session_id": ids["filler_session_id"]},
        )
        await db.execute(
            text("DELETE FROM chat_sessions WHERE id = :filler_session_id"),
            {"filler_session_id": ids["filler_session_id"]},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_supported_delete_route_rolls_back_intentional_session_restrict_edge(
    owner_sessionmaker, monkeypatch
) -> None:
    """A child branch keeps its parent intact and surfaces an explicit conflict."""
    suffix = uuid.uuid4().hex[:10]
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"Delete Restrict Tenant {suffix}", slug=f"restrict-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"restrict-{suffix}",
                email=f"restrict-{suffix}@example.test",
                password_hash="x",
                display_name="Delete Restrict Owner",
                tenant_id=tenant_id,
                role="member",
            )
        )
        await db.flush()
        agent = Agent(
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name=f"Delete Restrict Agent {suffix}",
            role_description="Exercises an intentional Session restrict edge.",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        parent = ChatSession(agent_id=agent.id, user_id=user_id, tenant_id=tenant_id, title=f"parent-{suffix}")
        db.add(parent)
        await db.flush()
        child = ChatSession(
            agent_id=agent.id,
            user_id=user_id,
            tenant_id=tenant_id,
            title=f"child-{suffix}",
            parent_session_id=parent.id,
            root_session_id=parent.id,
        )
        db.add(child)
        await db.commit()
        agent_id = agent.id
        parent_id = parent.id
        child_id = child.id

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id, creator_id=user_id, tenant_id=tenant_id), "use"

    async def fake_authorize_loaded_session(**_kwargs):
        return "session_owner"

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access, raising=False)
    monkeypatch.setattr(chat_sessions_api, "_authorize_loaded_session", fake_authorize_loaded_session, raising=False)

    async with owner_sessionmaker() as db:
        with pytest.raises(chat_sessions_api.HTTPException) as exc:
            await chat_sessions_api.delete_session(
                agent_id=agent_id,
                session_id=parent_id,
                current_user=SimpleNamespace(id=user_id, role="member"),
                db=db,
            )
        assert exc.value.status_code == 409
        await db.rollback()
        remaining = await db.scalars(select(ChatSession.id).where(ChatSession.id.in_([parent_id, child_id])))
        assert set(remaining) == {parent_id, child_id}
