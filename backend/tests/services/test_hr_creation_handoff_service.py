from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_handoff_authority(owner_sessionmaker, *, include_hr: bool = True):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, source_agent_id, source_session_id, source_run_id = (uuid.uuid4() for _ in range(5))
    hr_agent_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="HR Handoff Tenant", slug=f"hr-handoff-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"handoff-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@hr-handoff.test",
                password_hash="x",
                display_name="HR Handoff Requester",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=source_agent_id,
                tenant_id=tenant_id,
                name="Planning Agent",
                role_description="Turns user intent into governed work briefs.",
                creator_id=user_id,
                owner_user_id=user_id,
                agent_class="internal_tenant",
                status="idle",
            )
        )
        if include_hr:
            db.add(
                Agent(
                    id=hr_agent_id,
                    tenant_id=tenant_id,
                    name="__system_hr__",
                    role_description="System HR",
                    creator_id=user_id,
                    owner_user_id=user_id,
                    agent_class="internal_system",
                    status="idle",
                )
            )
        await db.flush()
        db.add(
            ChatSession(
                id=source_session_id,
                agent_id=source_agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Source request",
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        db.add(
            RuntimeTask(
                id=source_run_id,
                task_type="web_chat_turn",
                status="running",
                parent_agent_id=source_agent_id,
                child_agent_id=source_agent_id,
                tenant_id=tenant_id,
                parent_session_id=str(source_session_id),
                child_session_id=str(source_session_id),
                root_user_id=user_id,
                root_session_id=str(source_session_id),
                root_runtime_task_id=source_run_id,
                prompt="Please start a governed HR handoff.",
                metadata_json={"turn_id": f"turn-{source_run_id.hex}"},
            )
        )
        await db.commit()
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "source_agent_id": source_agent_id,
        "source_session_id": source_session_id,
        "source_run_id": source_run_id,
        "hr_agent_id": hr_agent_id if include_hr else None,
    }


async def _start(owner_sessionmaker, authority, brief: str):
    from app.services.hr_creation_handoff_service import start_hr_creation_handoff

    async with owner_sessionmaker() as db:
        return await start_hr_creation_handoff(
            db,
            tenant_id=authority["tenant_id"],
            requester_user_id=authority["user_id"],
            source_agent_id=authority["source_agent_id"],
            source_session_id=authority["source_session_id"],
            source_runtime_task_id=authority["source_run_id"],
            creation_brief=brief,
        )


async def test_handoff_creates_one_hr_session_and_one_run_across_replay(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput

    authority = await _seed_handoff_authority(owner_sessionmaker)
    brief = "Create a release-feedback employee; do not deploy, mutate production data, or send externally."

    first = await _start(owner_sessionmaker, authority, brief)
    second = await _start(owner_sessionmaker, authority, brief)

    assert first["status"] == "hr_handoff_started"
    assert first["hr_agent_id"] == str(authority["hr_agent_id"])
    assert second == {**first, "replayed": True}
    assert first["replayed"] is False

    hr_session_id = uuid.UUID(first["hr_session_id"])
    async with owner_sessionmaker() as db:
        hr_session = await db.get(ChatSession, hr_session_id)
        assert hr_session is not None
        assert hr_session.agent_id == authority["hr_agent_id"]
        assert hr_session.user_id == authority["user_id"]
        evidence = dict(hr_session.transcript_metadata_json or {})["hr_creation_handoff"]
        assert evidence == {
            "schema": "hive.hr_creation_handoff.v1",
            "source_agent_id": str(authority["source_agent_id"]),
            "source_session_id": str(authority["source_session_id"]),
            "source_runtime_task_id": str(authority["source_run_id"]),
            "requester_user_id": str(authority["user_id"]),
            "creation_brief_sha256": first["creation_brief_sha256"],
        }
        assert (
            await db.scalar(select(func.count()).select_from(Agent).where(Agent.tenant_id == authority["tenant_id"]))
            == 2
        )
        assert (
            await db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.id == hr_session_id)) == 1
        )
        assert (
            await db.scalar(
                select(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == str(hr_session_id))
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count()).select_from(SessionTurnInput).where(SessionTurnInput.session_id == hr_session_id)
            )
            == 1
        )
        input_row = await db.get(SessionTurnInput, uuid.uuid5(hr_session_id, "initial-input"))
        assert input_row is not None
        assert input_row.content_parts_json[0]["display_content"] == brief
        model_payload = __import__("json").loads(input_row.content_parts_json[0]["text"])
        assert model_payload["creation_brief"] == brief
        assert model_payload["provenance"] == {
            "source": "authenticated_agent_handoff",
            "source_agent_name": "Planning Agent",
        }
        accepted = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == hr_session_id,
                ChatTranscriptEvent.item_kind == "human_input",
                ChatTranscriptEvent.lifecycle == "accepted",
            )
        )
        assert accepted is not None
        assert accepted.metadata_json["v2_payload"]["content_parts"] == input_row.content_parts_json


async def test_handoff_advisory_lock_makes_concurrent_replay_single_effect(owner_sessionmaker) -> None:
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask

    authority = await _seed_handoff_authority(owner_sessionmaker)
    brief = "Create a bounded test-report employee and wait for exact HR confirmation."

    first, second = await asyncio.gather(
        _start(owner_sessionmaker, authority, brief),
        _start(owner_sessionmaker, authority, brief),
    )

    assert first["hr_session_id"] == second["hr_session_id"]
    assert sorted([first["replayed"], second["replayed"]]) == [False, True]
    assert {first["status"], second["status"]} <= {"hr_handoff_started", "hr_handoff_queued"}
    assert "hr_handoff_started" in {first["status"], second["status"]}
    hr_session_id = uuid.UUID(first["hr_session_id"])
    async with owner_sessionmaker() as db:
        assert (
            await db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.id == hr_session_id)) == 1
        )
        assert (
            await db.scalar(
                select(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == str(hr_session_id))
            )
            == 1
        )


async def test_handoff_creator_returns_queued_when_replay_owns_dispatch_lease(owner_sessionmaker, monkeypatch) -> None:
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.services import hr_creation_handoff_service
    from app.services.hr_creation_handoff_service import HrCreationHandoffError

    authority = await _seed_handoff_authority(owner_sessionmaker)

    async def replay_owned_lease_receipt(**kwargs):
        # The creator's durable accept commit released the handoff advisory
        # lock; the exact concurrent replay now owns the Hook/dispatch lease,
        # so this creator observes an in-flight admission with no visible run.
        await kwargs["db"].commit()
        return {"admission_state": "hook_running", "status": "accepted", "run": None, "replayed": False}

    monkeypatch.setattr(hr_creation_handoff_service, "_submit_initial_handoff", replay_owned_lease_receipt)
    queued = await _start(owner_sessionmaker, authority, "Create an onboarding-buddy employee for the support team.")
    assert queued["status"] == "hr_handoff_queued"
    assert queued["replayed"] is False
    hr_session_id = uuid.UUID(queued["hr_session_id"])
    async with owner_sessionmaker() as db:
        hr_session = await db.get(ChatSession, hr_session_id)
        assert hr_session is not None
        assert (hr_session.transcript_metadata_json or {})["hr_creation_handoff"] == {
            "schema": "hive.hr_creation_handoff.v1",
            "source_agent_id": str(authority["source_agent_id"]),
            "source_session_id": str(authority["source_session_id"]),
            "source_runtime_task_id": str(authority["source_run_id"]),
            "requester_user_id": str(authority["user_id"]),
            "creation_brief_sha256": queued["creation_brief_sha256"],
        }
        assert (
            await db.scalar(
                select(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == str(hr_session_id))
            )
            == 0
        )

    async def rejected_receipt(**kwargs):
        # Dispatch can terminally reject an admitted input (for example
        # active_turn_conflict_after_admission): the input row status is
        # rejected while the admission state stays admitted and no run exists.
        await kwargs["db"].commit()
        return {"admission_state": "admitted", "status": "rejected", "run": None, "replayed": False}

    monkeypatch.setattr(hr_creation_handoff_service, "_submit_initial_handoff", rejected_receipt)
    with pytest.raises(HrCreationHandoffError, match="handoff_start_failed") as exc_info:
        await _start(owner_sessionmaker, authority, "Create a separate billing-audit employee.")
    assert exc_info.value.reason_code == "handoff_start_failed"


async def test_handoff_fails_closed_without_system_hr_or_matching_web_authority(owner_sessionmaker) -> None:
    from app.models.chat_session import ChatSession
    from app.services.hr_creation_handoff_service import HrCreationHandoffError

    missing_hr = await _seed_handoff_authority(owner_sessionmaker, include_hr=False)
    with pytest.raises(HrCreationHandoffError, match="hr_agent_unavailable") as missing_error:
        await _start(owner_sessionmaker, missing_hr, "Create a test employee.")
    assert missing_error.value.reason_code == "hr_agent_unavailable"

    authority = await _seed_handoff_authority(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        source_session = await db.get(ChatSession, authority["source_session_id"])
        assert source_session is not None
        source_session.source_channel = "agent"
        await db.commit()
    with pytest.raises(HrCreationHandoffError, match="source_session_not_direct_user") as authority_error:
        await _start(owner_sessionmaker, authority, "Create another test employee.")
    assert authority_error.value.reason_code == "source_session_not_direct_user"

    async with owner_sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatSession)
                .where(
                    ChatSession.agent_id == authority["hr_agent_id"],
                    ChatSession.id != authority["source_session_id"],
                )
            )
            == 0
        )
