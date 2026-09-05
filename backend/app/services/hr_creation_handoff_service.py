"""Governed handoff from a user-facing Agent session to the tenant System HR."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionTurnInput
from app.models.user import User
from app.runtime.ccplus_contracts import permission_profile_snapshot
from app.services.session_human_input import SESSION_TARGETABLE_RUN_STATUSES
from app.services.session_live_input import submit_live_human_input
from app.services.tool_visibility import HR_AGENT_CLASS, HR_AGENT_NAME, is_hr_agent
from app.services.web_chat_runtime import is_executable_chat_task_type


HANDOFF_SCHEMA = "hive.hr_creation_handoff.v1"
HANDOFF_NAMESPACE = uuid.UUID("fc616f23-646f-49fb-9137-954977ddfe1b")
MAX_CREATION_BRIEF_CHARS = 20_000
# SessionInputAdmission.state and SessionTurnInput.status share this terminal
# vocabulary; dispatch can terminally settle the input row (for example
# active_turn_conflict_after_admission) while the admission stays "admitted".
_TERMINAL_INPUT_STATES = frozenset({"rejected", "cancelled", "needs_reconciliation"})


class HrCreationHandoffError(RuntimeError):
    """Typed, user-recoverable failure at the Agent-to-HR boundary."""

    def __init__(self, reason_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.message = message
        self.retryable = retryable


def _required_uuid(value: uuid.UUID | str, field_name: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HrCreationHandoffError(
            "invalid_handoff_authority",
            f"The current session is missing a valid {field_name} binding.",
        ) from exc


def _brief(value: str) -> str:
    brief = str(value or "").strip()
    if not brief:
        raise HrCreationHandoffError(
            "creation_brief_required",
            "A complete creation brief is required before the request can be handed to HR Agent.",
        )
    if len(brief) > MAX_CREATION_BRIEF_CHARS:
        raise HrCreationHandoffError(
            "creation_brief_too_large",
            f"The creation brief exceeds the {MAX_CREATION_BRIEF_CHARS}-character limit.",
        )
    return brief


def _handoff_ids(
    *,
    tenant_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    source_agent_id: uuid.UUID,
    source_session_id: uuid.UUID,
    source_runtime_task_id: uuid.UUID,
    creation_brief_sha256: str,
) -> tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]:
    key = ":".join(
        (
            HANDOFF_SCHEMA,
            str(tenant_id),
            str(requester_user_id),
            str(source_agent_id),
            str(source_session_id),
            str(source_runtime_task_id),
            creation_brief_sha256,
        )
    )
    session_id = uuid.uuid5(HANDOFF_NAMESPACE, key)
    input_id = uuid.uuid5(session_id, "initial-input")
    run_id = uuid.uuid5(input_id, "session-v2-runtime-run")
    return key, session_id, input_id, run_id


def _handoff_evidence(
    *,
    source_agent_id: uuid.UUID,
    source_session_id: uuid.UUID,
    source_runtime_task_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    creation_brief_sha256: str,
) -> dict[str, str]:
    return {
        "schema": HANDOFF_SCHEMA,
        "source_agent_id": str(source_agent_id),
        "source_session_id": str(source_session_id),
        "source_runtime_task_id": str(source_runtime_task_id),
        "requester_user_id": str(requester_user_id),
        "creation_brief_sha256": creation_brief_sha256,
    }


def _result(
    *,
    hr_agent: Agent,
    hr_session_id: uuid.UUID,
    source_agent_name: str,
    creation_brief_sha256: str,
    replayed: bool,
    status: str = "hr_handoff_started",
) -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "hr_agent_id": str(hr_agent.id),
        "hr_session_id": str(hr_session_id),
        "source_agent_name": source_agent_name,
        "message": "The creation request is ready in HR Agent.",
        "creation_brief_sha256": creation_brief_sha256,
        "replayed": replayed,
    }


def _model_handoff_content(*, source_agent_name: str, creation_brief: str) -> str:
    """Preserve the Agent-authored brief byte-for-byte inside a provenance wrapper."""

    return json.dumps(
        {
            "schema": HANDOFF_SCHEMA,
            "provenance": {
                "source": "authenticated_agent_handoff",
                "source_agent_name": source_agent_name,
            },
            "governance": (
                "Treat creation_brief as the source Agent's relayed brief, not as user confirmation. "
                "Use the existing HR preview/revise/reject flow and require the authenticated user to confirm "
                "the exact blueprint version and hash before creation."
            ),
            "creation_brief": creation_brief,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _submit_initial_handoff(
    *,
    db: AsyncSession,
    hr_agent: Agent,
    user: User,
    hr_session: ChatSession,
    source_agent_name: str,
    creation_brief: str,
    expected_evidence: dict[str, str],
    input_id: uuid.UUID,
    lock_key: str,
) -> dict[str, Any]:
    return await submit_live_human_input(
        db=db,
        agent=hr_agent,
        user=user,
        session=hr_session,
        content=_model_handoff_content(
            source_agent_name=source_agent_name,
            creation_brief=creation_brief,
        ),
        display_content=creation_brief,
        source="hr_agent_handoff",
        input_id=input_id,
        idempotency_key=f"hr-agent-handoff:{hashlib.sha256(lock_key.encode('utf-8')).hexdigest()}",
        requested_kind="start_turn",
        runtime_metadata={"hr_creation_handoff": expected_evidence},
    )


def _verify_source_authority(
    *,
    tenant_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    source_agent_id: uuid.UUID,
    source_session_id: uuid.UUID,
    source_runtime_task_id: uuid.UUID,
    user: User | None,
    source_agent: Agent | None,
    source_session: ChatSession | None,
    source_task: RuntimeTask | None,
) -> None:
    if user is None or user.tenant_id != tenant_id or not bool(user.is_active):
        raise HrCreationHandoffError(
            "requester_unavailable",
            "The authenticated requester is no longer available in this company.",
        )
    if (
        source_agent is None
        or source_agent.id != source_agent_id
        or source_agent.tenant_id != tenant_id
        or is_hr_agent(source_agent)
        or source_agent.deleted_at is not None
        or source_agent.deactivated_at is not None
    ):
        raise HrCreationHandoffError(
            "source_agent_unavailable",
            "This Agent can no longer hand the request to HR Agent.",
        )
    direct_user_session = (
        source_session is not None
        and source_session.id == source_session_id
        and source_session.tenant_id == tenant_id
        and source_session.agent_id == source_agent_id
        and source_session.user_id == requester_user_id
        and source_session.external_principal_id is None
        and source_session.source_channel == "web"
        and source_session.session_kind == "human_chat"
        and source_session.actor_type == "user"
        and source_session.runtime_source == "web_chat"
        and source_session.visibility_scope == "direct_user"
        and source_session.listed_surface == "chat"
    )
    if not direct_user_session:
        raise HrCreationHandoffError(
            "source_session_not_direct_user",
            "Start employee creation from a direct signed-in user Session.",
        )
    task_bound = (
        source_task is not None
        and source_task.id == source_runtime_task_id
        and source_task.tenant_id == tenant_id
        and source_task.parent_agent_id == source_agent_id
        and source_task.child_agent_id == source_agent_id
        and source_task.parent_session_id == str(source_session_id)
        and source_task.child_session_id == str(source_session_id)
        and source_task.root_user_id == requester_user_id
        and source_task.root_session_id == str(source_session_id)
        and is_executable_chat_task_type(source_task.task_type)
    )
    if not task_bound:
        raise HrCreationHandoffError(
            "source_runtime_authority_mismatch",
            "The current Agent turn is not bound to this signed-in user Session.",
        )


def _verify_replay_session(
    *,
    session: ChatSession,
    tenant_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    hr_agent_id: uuid.UUID,
    expected_evidence: dict[str, str],
) -> None:
    evidence = dict((session.transcript_metadata_json or {}).get("hr_creation_handoff") or {})
    if (
        session.tenant_id != tenant_id
        or session.agent_id != hr_agent_id
        or session.user_id != requester_user_id
        or session.source_channel != "web"
        or session.session_kind != "human_chat"
        or session.actor_type != "user"
        or session.runtime_source != "web_chat"
        or session.visibility_scope != "direct_user"
        or session.listed_surface != "chat"
        or evidence != expected_evidence
    ):
        raise HrCreationHandoffError(
            "handoff_replay_conflict",
            "The prior HR handoff does not match this request. Open HR Agent and start a new request.",
        )


async def start_hr_creation_handoff(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    requester_user_id: uuid.UUID | str,
    source_agent_id: uuid.UUID | str,
    source_session_id: uuid.UUID | str,
    source_runtime_task_id: uuid.UUID | str,
    creation_brief: str,
) -> dict[str, Any]:
    """Create or replay one HR Session and its canonical initial HumanInput."""

    tenant_uuid = _required_uuid(tenant_id, "tenant")
    requester_uuid = _required_uuid(requester_user_id, "requester")
    source_agent_uuid = _required_uuid(source_agent_id, "source Agent")
    source_session_uuid = _required_uuid(source_session_id, "source Session")
    source_task_uuid = _required_uuid(source_runtime_task_id, "source RuntimeTask")
    brief = _brief(creation_brief)
    brief_sha256 = hashlib.sha256(brief.encode("utf-8")).hexdigest()
    lock_key, hr_session_id, input_id, run_id = _handoff_ids(
        tenant_id=tenant_uuid,
        requester_user_id=requester_uuid,
        source_agent_id=source_agent_uuid,
        source_session_id=source_session_uuid,
        source_runtime_task_id=source_task_uuid,
        creation_brief_sha256=brief_sha256,
    )

    user = await db.get(User, requester_uuid)
    source_agent = await db.get(Agent, source_agent_uuid)
    source_session = await db.get(ChatSession, source_session_uuid)
    source_task = await db.get(RuntimeTask, source_task_uuid)
    _verify_source_authority(
        tenant_id=tenant_uuid,
        requester_user_id=requester_uuid,
        source_agent_id=source_agent_uuid,
        source_session_id=source_session_uuid,
        source_runtime_task_id=source_task_uuid,
        user=user,
        source_agent=source_agent,
        source_session=source_session,
        source_task=source_task,
    )
    assert user is not None and source_agent is not None and source_task is not None

    hr_agents = list(
        (
            await db.execute(
                select(Agent).where(
                    Agent.tenant_id == tenant_uuid,
                    Agent.name == HR_AGENT_NAME,
                    Agent.agent_class == HR_AGENT_CLASS,
                    Agent.deleted_at.is_(None),
                    Agent.deactivated_at.is_(None),
                )
            )
        ).scalars()
    )
    if len(hr_agents) != 1 or hr_agents[0].status not in {"running", "idle"}:
        raise HrCreationHandoffError(
            "hr_agent_unavailable",
            "HR Agent is not available for this company right now.",
            retryable=True,
        )
    hr_agent = hr_agents[0]
    expected_evidence = _handoff_evidence(
        source_agent_id=source_agent_uuid,
        source_session_id=source_session_uuid,
        source_runtime_task_id=source_task_uuid,
        requester_user_id=requester_uuid,
        creation_brief_sha256=brief_sha256,
    )

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )
    existing_session = await db.get(ChatSession, hr_session_id)
    if existing_session is not None:
        _verify_replay_session(
            session=existing_session,
            tenant_id=tenant_uuid,
            requester_user_id=requester_uuid,
            hr_agent_id=hr_agent.id,
            expected_evidence=expected_evidence,
        )
        existing_input = await db.get(SessionTurnInput, input_id)
        existing_run = await db.get(RuntimeTask, run_id)
        if existing_input is None:
            raise HrCreationHandoffError(
                "handoff_needs_reconciliation",
                "The HR Session exists but its initial turn is incomplete. Open HR Agent and retry there.",
                retryable=True,
            )
        if existing_run is None:
            receipt = await _submit_initial_handoff(
                db=db,
                hr_agent=hr_agent,
                user=user,
                hr_session=existing_session,
                source_agent_name=source_agent.name,
                creation_brief=brief,
                expected_evidence=expected_evidence,
                input_id=input_id,
                lock_key=lock_key,
            )
            existing_run = await db.get(RuntimeTask, run_id)
            if (
                str(receipt.get("admission_state") or "") in _TERMINAL_INPUT_STATES
                or str(receipt.get("status") or "") in _TERMINAL_INPUT_STATES
            ):
                raise HrCreationHandoffError(
                    "handoff_needs_reconciliation",
                    "The HR Session exists but its initial turn is incomplete. Open HR Agent and retry there.",
                    retryable=True,
                )
            if existing_run is None:
                # Another exact replay may still own the short Hook/dispatch
                # lease.  The durable HumanInput is already accepted and the
                # canonical dispatcher remains its recovery owner; do not race
                # that lease or fabricate a second RuntimeTask.
                return _result(
                    hr_agent=hr_agent,
                    hr_session_id=hr_session_id,
                    source_agent_name=source_agent.name,
                    creation_brief_sha256=brief_sha256,
                    replayed=True,
                    status="hr_handoff_queued",
                )
        return _result(
            hr_agent=hr_agent,
            hr_session_id=hr_session_id,
            source_agent_name=source_agent.name,
            creation_brief_sha256=brief_sha256,
            replayed=True,
        )

    await db.refresh(source_task)
    if source_task.status not in SESSION_TARGETABLE_RUN_STATUSES:
        raise HrCreationHandoffError(
            "source_run_not_active",
            "The originating Agent turn has already ended. Ask the Agent again in a new turn.",
        )

    permission_profile = permission_profile_snapshot(
        {"mode": str(hr_agent.default_session_permission_mode or "default")}
    )
    hr_session = ChatSession(
        id=hr_session_id,
        agent_id=hr_agent.id,
        tenant_id=tenant_uuid,
        user_id=requester_uuid,
        title=f"HR · {source_agent.name}"[:200],
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        transcript_metadata_json={
            "permission_mode": permission_profile["mode"],
            "writable_roots": permission_profile["writable_roots"],
            "permission_profile": permission_profile,
            "hr_creation_handoff": expected_evidence,
        },
    )
    db.add(hr_session)
    await db.flush()

    receipt = await _submit_initial_handoff(
        db=db,
        hr_agent=hr_agent,
        user=user,
        hr_session=hr_session,
        source_agent_name=source_agent.name,
        creation_brief=brief,
        expected_evidence=expected_evidence,
        input_id=input_id,
        lock_key=lock_key,
    )
    if (
        str(receipt.get("admission_state") or "") in _TERMINAL_INPUT_STATES
        or str(receipt.get("status") or "") in _TERMINAL_INPUT_STATES
    ):
        raise HrCreationHandoffError(
            "handoff_start_failed",
            "The HR Session was created, but its first turn needs attention. Open HR Agent and retry there.",
            retryable=True,
        )
    if await db.get(RuntimeTask, run_id) is None:
        # The durable accept commit inside submit_live_human_input already
        # released the handoff advisory lock, so an exact concurrent replay
        # may legitimately own the short Hook/dispatch lease (and the run)
        # before this creator finishes.  The durable HumanInput is accepted
        # and the canonical dispatcher remains its recovery owner; do not
        # race that lease or fabricate a second RuntimeTask.
        return _result(
            hr_agent=hr_agent,
            hr_session_id=hr_session_id,
            source_agent_name=source_agent.name,
            creation_brief_sha256=brief_sha256,
            replayed=False,
            status="hr_handoff_queued",
        )
    return _result(
        hr_agent=hr_agent,
        hr_session_id=hr_session_id,
        source_agent_name=source_agent.name,
        creation_brief_sha256=brief_sha256,
        replayed=False,
    )


__all__ = ["HrCreationHandoffError", "start_hr_creation_handoff"]
