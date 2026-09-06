from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from typing import Any
import uuid

import pytest
from sqlalchemy import func, select


pytestmark = pytest.mark.asyncio


def _claimed(*, event_kind: str = "turn_stop", terminal_status: str = "completed"):
    from app.services.runtime_terminal_boundary_outbox import ClaimedTerminalBoundary

    tenant_id, task_id, agent_id, session_id, boundary_id, event_id = (uuid.uuid4() for _ in range(6))
    binding = {
        "tenant_id": str(tenant_id),
        "runtime_task_id": str(task_id),
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "authority_ref": "runtime_task",
        "authority_id": str(task_id),
        "authority_sha256": "a" * 64,
    }
    if event_kind != "runtime_terminal":
        binding.update(
            {
                "terminal_event_id": str(event_id),
                "terminal_sequence": 17,
                "terminal_event_sha256": "b" * 64,
            }
        )
    return ClaimedTerminalBoundary(
        id=boundary_id,
        tenant_id=tenant_id,
        runtime_task_id=task_id,
        agent_id=agent_id,
        session_id=str(session_id),
        event_kind=event_kind,
        terminal_status=terminal_status,
        authority_ref="runtime_task",
        authority_id=str(task_id),
        binding=binding,
        binding_sha256="c" * 64,
        idempotency_key="d" * 64,
        claim_token=uuid.uuid4(),
        attempt=1,
    )


def _material(
    item,
    *,
    event_kind: str | None = None,
    source: str = "task",
    task_type: str = "business_task",
    custom_event=None,
    projection_payload: dict[str, Any] | None = None,
):
    from app.services.direct_invocation_terminal_boundary_processor import _DirectTerminalMaterial

    kind = event_kind or item.event_kind
    terminal_event_id = None
    terminal_sequence = None
    response_payload = None
    response_commit = None
    if kind != "runtime_terminal":
        terminal_event_id = uuid.UUID(str(item.binding["terminal_event_id"]))
        terminal_sequence = int(item.binding["terminal_sequence"])
    if kind == "turn_stop":
        response_payload = {
            "agent_id": str(item.agent_id),
            "session_id": item.session_id,
            "source": source,
            "messages": [{"role": "user", "content": "Finish the delegated work."}],
            "metadata": {
                "tenant_id": str(item.tenant_id),
                "final_response": "Done.",
            },
        }
        response_commit = {
            "schema": "hive.response_commit.v1",
            "committed": True,
            "commit_kind": "runtime_terminal_boundary",
            "idempotency_key": item.idempotency_key,
        }
    return _DirectTerminalMaterial(
        tenant_id=item.tenant_id,
        runtime_task_id=item.runtime_task_id,
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_kind=kind,
        terminal_status=item.terminal_status,
        source=source,
        turn_id=f"turn-{item.runtime_task_id.hex}",
        terminal_event_id=terminal_event_id,
        terminal_sequence=terminal_sequence,
        response_payload=response_payload,
        response_commit=response_commit,
        source_refs=(
            f"runtime-terminal-boundary://{item.id}",
            f"runtime-task://{item.runtime_task_id}",
        ),
        hook_metadata={"task_type": task_type},
        custom_event=custom_event,
        projection_payload=dict(projection_payload or {}),
    )


def _runtime_task(*, task_type: str, status: str, metadata: dict[str, Any] | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        task_type=task_type,
        status=status,
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        child_session_id=str(uuid.uuid4()),
        metadata_json=dict(metadata or {}),
        writer_generation=1,
        config_snapshot_hash=None,
        policy_snapshot_hash=None,
    )


async def _async_value(value):
    return value


async def test_runtime_terminal_returns_receipt_without_hooks_or_t0(monkeypatch) -> None:
    from app.services.direct_invocation_terminal_boundary_processor import (
        DirectInvocationTerminalBoundaryProcessor,
    )

    item = _claimed(event_kind="runtime_terminal", terminal_status="skipped")
    material = _material(item)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("runtime_terminal must not invoke projections or hooks")

    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=forbidden,
        turn_boundary_projector=forbidden,
        emit_advisory_hook=forbidden,
        response_projector=forbidden,
        seal_t0=lambda **_kwargs: pytest.fail("runtime_terminal must not seal T0"),
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))

    receipt = await processor(item)

    assert receipt == {
        "boundary_id": str(item.id),
        "source_refs": [
            f"runtime-terminal-boundary://{item.id}",
            f"runtime-task://{item.runtime_task_id}",
        ],
    }


async def test_delegation_runtime_terminal_projects_parent_before_receipt(monkeypatch) -> None:
    from app.services.direct_invocation_terminal_boundary_processor import (
        DirectInvocationTerminalBoundaryProcessor,
    )

    item = _claimed(event_kind="runtime_terminal", terminal_status="skipped")
    material = _material(item, task_type="delegation", source="agent")
    calls: list[str] = []

    async def project_parent(_material):
        calls.append("parent")
        return {"parent_event_id": "parent-event", "notification_id": "notification"}

    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: pytest.fail("runtime_terminal must not bridge T0"),
        turn_boundary_projector=lambda _ctx: pytest.fail("runtime_terminal must not project TURN_STOP"),
        emit_advisory_hook=lambda *_args, **_kwargs: pytest.fail("runtime_terminal must not emit hooks"),
        response_projector=lambda _ctx: pytest.fail("runtime_terminal must not project RESPONSE_COMPLETE"),
        delegation_parent_projector=project_parent,
        seal_t0=lambda **_kwargs: pytest.fail("runtime_terminal must not seal T0"),
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))

    receipt = await processor(item)

    assert calls == ["parent"]
    assert len(receipt["result_content_sha256"]) == 64


@pytest.mark.parametrize(
    ("source", "task_type", "custom_event_name"),
    [("trigger", "trigger", "trigger_end"), ("agent", "delegation", "delegation_end")],
)
async def test_turn_stop_orders_required_projections_before_custom_terminal(
    monkeypatch,
    source,
    task_type,
    custom_event_name,
) -> None:
    from app.runtime.hooks import HookEvent
    from app.services.direct_invocation_terminal_boundary_processor import (
        DirectInvocationTerminalBoundaryProcessor,
    )

    item = _claimed()
    custom_event = HookEvent(custom_event_name)
    material = _material(
        item,
        source=source,
        task_type=task_type,
        custom_event=custom_event,
    )
    order: list[str] = []

    async def bridge(**_kwargs):
        order.append("t0_bridge")
        return True

    async def turn(ctx):
        assert ctx.event is HookEvent.TURN_STOP
        order.append("required_turn")

    async def response(ctx):
        assert ctx.event is HookEvent.RESPONSE_COMPLETE
        order.append("response_complete")
        return {"receipt_sha256": "e" * 64}

    async def advisory(event, **_kwargs):
        if event is custom_event:
            order.append(custom_event_name)

    async def project_parent(_material):
        if task_type == "delegation":
            order.append("parent_completion")
            return {"parent_event_id": "parent-event", "notification_id": "notification"}
        return None

    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=bridge,
        turn_boundary_projector=turn,
        emit_advisory_hook=advisory,
        response_projector=response,
        delegation_parent_projector=project_parent,
        seal_t0=lambda **_kwargs: SimpleNamespace(
            boundary_id=item.id,
            event_id=f"evt_{'1' * 32}",
            sequence=21,
        ),
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))

    receipt = await processor(item)

    expected_parent = ["parent_completion"] if task_type == "delegation" else []
    assert order == ["t0_bridge", "required_turn", "response_complete", *expected_parent, custom_event_name]
    assert receipt["terminal_event_id"] == str(material.terminal_event_id)
    assert len(receipt["response_projection_sha256"]) == 64
    if task_type == "delegation":
        assert len(receipt["result_content_sha256"]) == 64


async def test_turn_abort_never_projects_response_complete(monkeypatch) -> None:
    from app.runtime.hooks import HookEvent
    from app.services.direct_invocation_terminal_boundary_processor import (
        DirectInvocationTerminalBoundaryProcessor,
    )

    item = _claimed(event_kind="turn_abort", terminal_status="failed")
    material = _material(
        item,
        source="agent",
        task_type="delegation",
        custom_event=HookEvent.DELEGATION_END,
    )
    required: list[str] = []
    advisory: list[str] = []

    async def turn(ctx):
        required.append(ctx.event.value)

    async def emit(event, **_kwargs):
        advisory.append(event.value)

    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=turn,
        emit_advisory_hook=emit,
        response_projector=lambda _ctx: pytest.fail("turn_abort must not project RESPONSE_COMPLETE"),
        seal_t0=lambda **_kwargs: SimpleNamespace(
            boundary_id=item.id,
            event_id=f"evt_{'2' * 32}",
            sequence=22,
        ),
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))

    receipt = await processor(item)

    assert required == ["turn_abort"]
    assert advisory == ["turn_abort", "delegation_end"]
    assert "response_projection_sha256" not in receipt


async def test_delegation_parent_projection_failure_prevents_terminal_ack_receipt(monkeypatch) -> None:
    from app.runtime.hooks import HookEvent
    from app.services.direct_invocation_terminal_boundary_processor import (
        DirectInvocationTerminalBoundaryProcessor,
    )

    item = _claimed()
    material = _material(
        item,
        source="agent",
        task_type="delegation",
        custom_event=HookEvent.DELEGATION_END,
    )
    advisory: list[str] = []

    async def fail_parent(_material):
        raise RuntimeError("parent projection unavailable")

    async def emit(event, **_kwargs):
        advisory.append(event.value)

    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=lambda _ctx: _async_value(None),
        emit_advisory_hook=emit,
        response_projector=lambda _ctx: _async_value({"receipt_sha256": "e" * 64}),
        delegation_parent_projector=fail_parent,
        seal_t0=lambda **_kwargs: SimpleNamespace(
            boundary_id=item.id,
            event_id=f"evt_{'2' * 32}",
            sequence=22,
        ),
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))

    with pytest.raises(RuntimeError, match="parent projection unavailable"):
        await processor(item)

    assert HookEvent.DELEGATION_END.value not in advisory


@pytest.mark.parametrize("missing", ["assistant", "payload"])
async def test_completed_direct_invocation_without_authority_is_typed_pending(monkeypatch, missing) -> None:
    from app.services import direct_invocation_terminal_boundary_processor as module

    task = _runtime_task(
        task_type="business_task",
        status="completed",
        metadata={
            "terminal_reason": "turn_stop",
            "response_complete_payload": {
                "agent_id": "unused",
                "session_id": "unused",
                "source": "task",
                "messages": [],
                "metadata": {},
            },
        },
    )
    if missing == "payload":
        task.metadata_json.pop("response_complete_payload")
        terminal_event = SimpleNamespace()
    else:
        terminal_event = None
    monkeypatch.setattr(module, "_latest_run_event", lambda *_args, **_kwargs: _async_value(terminal_event))

    with pytest.raises(module.DirectInvocationTerminalBoundaryPending):
        await module._build_direct_terminal_spec(SimpleNamespace(), task)


async def test_non_direct_task_type_is_rejected_before_lookup() -> None:
    from app.services.direct_invocation_terminal_boundary_processor import (
        _build_direct_terminal_spec,
    )
    from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch

    with pytest.raises(TerminalBoundaryCanonicalMismatch, match="not a direct invocation lane"):
        await _build_direct_terminal_spec(SimpleNamespace(), SimpleNamespace(task_type="workflow"))


async def test_delegation_binding_uses_child_agent_authority(monkeypatch) -> None:
    from app.services import direct_invocation_terminal_boundary_processor as module

    task = _runtime_task(task_type="delegation", status="failed")
    monkeypatch.setattr(module, "_latest_run_event", lambda *_args, **_kwargs: _async_value(None))

    binding = await module.build_direct_terminal_boundary_binding(SimpleNamespace(), task)

    assert binding["agent_id"] == str(task.child_agent_id)
    assert binding["agent_id"] != str(task.parent_agent_id)


async def test_claimed_binding_drift_is_rejected(monkeypatch) -> None:
    from app.services import direct_invocation_terminal_boundary_processor as module
    from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch

    item = _claimed()
    drifted = {**dict(item.binding), "authority_sha256": "f" * 64}
    monkeypatch.setattr(
        module,
        "validate_direct_terminal_boundary",
        lambda *_args, **_kwargs: _async_value(drifted),
    )

    with pytest.raises(TerminalBoundaryCanonicalMismatch, match="no longer matches canonical hashes"):
        await module._load_terminal_material(SimpleNamespace(), item)


async def test_replay_reuses_stable_t0_and_response_receipts(monkeypatch, tmp_path) -> None:
    from app.runtime.hooks import HookEvent
    from app.memory.t0.ledger import append_t0_session_event
    from app.services.direct_invocation_terminal_boundary_processor import (
        DirectInvocationTerminalBoundaryProcessor,
    )

    item = _claimed()
    material = _material(
        item,
        source="trigger",
        task_type="trigger",
        custom_event=HookEvent.TRIGGER_END,
    )
    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_type="assistant_final.completed",
        role="assistant",
        content="Done.",
        runtime_task_id=item.runtime_task_id,
        metadata={"turn_id": material.turn_id},
        data_root=tmp_path,
    )

    async def response(_ctx):
        return {"receipt_sha256": "9" * 64}

    hook_runs: list[tuple[str, str]] = []

    async def advisory(event, **kwargs):
        hook_runs.append((event.value, kwargs["metadata"]["hook_run_id"]))

    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=lambda _ctx: _async_value(None),
        emit_advisory_hook=advisory,
        response_projector=response,
        data_root=tmp_path,
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))

    first = await processor(item)
    replay = await processor(replace(item, attempt=2))

    assert replay == first
    assert replay["t0_boundary_id"] == str(item.id)
    assert hook_runs[:3] == hook_runs[3:]
    assert len({hook_run_id for _event, hook_run_id in hook_runs[:3]}) == 3


async def test_trigger_artifact_and_dream_sidecars_recover_idempotently(monkeypatch, tmp_path) -> None:
    from app.memory.t0.ledger import append_t0_session_event
    from app.services import auto_dream, dream_runtime
    from app.services.direct_invocation_terminal_boundary_processor import (
        DirectInvocationTerminalBoundaryProcessor,
    )
    from app.services.trigger_artifacts import trigger_output_artifact_ref

    item = _claimed()
    trigger_id = uuid.uuid4()
    artifact = trigger_output_artifact_ref(str(item.runtime_task_id))
    material = replace(
        _material(item, source="trigger", task_type="trigger"),
        task_metadata={
            "trigger_settlement": {
                "schema": "trigger_runtime_settlement.v1",
                "runtime_task_id": str(item.runtime_task_id),
            },
            "output_artifact": artifact,
            "trigger_artifact_input": {
                "triggers": [
                    {
                        "id": str(trigger_id),
                        "name": "daily",
                        "type": "cron",
                        "config": {"trigger_class": "scheduled_job"},
                    }
                ],
                "metadata": {"execution_class": "scheduled_job"},
            },
        },
        result_summary="Done.",
    )
    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_type="assistant_final.completed",
        role="assistant",
        content="Done.",
        runtime_task_id=item.runtime_task_id,
        metadata={"turn_id": material.turn_id},
        data_root=tmp_path,
    )
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(auto_dream, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    dream_enqueue_attempts: list[dict[str, Any]] = []
    dream_jobs: list[str] = []

    async def enqueue_due_dream(**kwargs):
        dream_enqueue_attempts.append(kwargs)
        recovery_source = kwargs["recovery_source"]
        if recovery_source not in dream_jobs:
            dream_jobs.append(recovery_source)
            return SimpleNamespace(created=True)
        return SimpleNamespace(created=False)

    monkeypatch.setattr(dream_runtime, "enqueue_due_dream", enqueue_due_dream)
    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=lambda _ctx: _async_value(None),
        emit_advisory_hook=lambda *_args, **_kwargs: _async_value(None),
        response_projector=lambda _ctx: _async_value({"receipt_sha256": "9" * 64}),
        data_root=tmp_path,
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))

    await processor(item)
    await processor(replace(item, attempt=2))

    artifact_path = tmp_path / str(item.agent_id) / artifact["path"]
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    dream_state = json.loads(
        (tmp_path / str(item.agent_id) / "memory" / "control" / "auto_dream_state.json").read_text(encoding="utf-8")
    )
    assert payload["runtime_task_id"] == item.runtime_task_id.hex
    assert payload["final_reply"] == "Done."
    assert dream_state["sessions_since_dream"] == 1
    assert dream_jobs == [f"runtime_terminal_boundary:{item.runtime_task_id}"]
    assert len(dream_enqueue_attempts) == 2


async def test_trigger_artifact_failure_does_not_advance_or_enqueue_dream(monkeypatch, tmp_path) -> None:
    from app.memory.t0.ledger import append_t0_session_event
    from app.services import auto_dream, dream_runtime
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.trigger_artifacts import trigger_output_artifact_ref

    item = _claimed()
    material = replace(
        _material(item, source="trigger", task_type="trigger"),
        task_metadata={
            "trigger_settlement": {
                "schema": "trigger_runtime_settlement.v1",
                "runtime_task_id": str(item.runtime_task_id),
            },
            "output_artifact": trigger_output_artifact_ref(str(item.runtime_task_id)),
            "trigger_artifact_input": {
                "triggers": [{"id": str(uuid.uuid4()), "name": "daily", "type": "cron", "config": {}}],
                "metadata": {},
            },
        },
        result_summary="Done.",
    )
    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_type="assistant_final.completed",
        role="assistant",
        content="Done.",
        runtime_task_id=item.runtime_task_id,
        metadata={"turn_id": material.turn_id},
        data_root=tmp_path,
    )
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(auto_dream, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    dream_enqueues: list[dict[str, Any]] = []

    async def enqueue_due_dream(**kwargs):
        dream_enqueues.append(kwargs)

    monkeypatch.setattr(dream_runtime, "enqueue_due_dream", enqueue_due_dream)
    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=lambda _ctx: _async_value(None),
        emit_advisory_hook=lambda *_args, **_kwargs: _async_value(None),
        response_projector=lambda _ctx: _async_value({"receipt_sha256": "9" * 64}),
        data_root=tmp_path,
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))
    monkeypatch.setattr(
        processor,
        "_project_trigger_artifact",
        lambda _material: (_ for _ in ()).throw(OSError("artifact unavailable")),
    )

    for attempt in range(1, 4):
        with pytest.raises(OSError, match="artifact unavailable"):
            await processor(replace(item, attempt=attempt))

    assert dream_enqueues == []
    assert not (tmp_path / str(item.agent_id) / "memory" / "control" / "auto_dream_state.json").exists()


async def test_trigger_abort_retries_artifact_without_success_learning(monkeypatch, tmp_path) -> None:
    from app.memory.t0.ledger import append_t0_session_event
    from app.services.direct_invocation_terminal_boundary_processor import (
        DirectInvocationTerminalBoundaryProcessor,
    )
    from app.services.trigger_artifacts import trigger_output_artifact_ref

    item = _claimed(event_kind="turn_abort", terminal_status="needs_reconciliation")
    artifact = trigger_output_artifact_ref(str(item.runtime_task_id))
    material = replace(
        _material(item, event_kind="turn_abort", source="trigger", task_type="trigger"),
        task_metadata={
            "output_artifact": artifact,
            "trigger_artifact_input": {
                "triggers": [{"id": str(uuid.uuid4()), "name": "react", "type": "cron", "config": {}}],
                "final_reply": "Partial ReAct result.",
                "metadata": {"workflow_trigger_results": [{"status": "needs_reconciliation"}]},
            },
        },
        result_summary="Durable result committed: ref=runtime-result://placeholder.",
    )
    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_type="assistant_final.aborted",
        role="assistant",
        content="Partial ReAct result.",
        runtime_task_id=item.runtime_task_id,
        metadata={"turn_id": material.turn_id},
        data_root=tmp_path,
    )
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=lambda _ctx: _async_value(None),
        emit_advisory_hook=lambda *_args, **_kwargs: _async_value(None),
        response_projector=lambda _ctx: pytest.fail("turn_abort must not project RESPONSE_COMPLETE"),
        data_root=tmp_path,
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))

    await processor(item)

    artifact_path = tmp_path / str(item.agent_id) / artifact["path"]
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["final_reply"] == "Partial ReAct result."
    assert not (tmp_path / str(item.agent_id) / "memory" / "control" / "auto_dream_state.json").exists()


async def _seed_postgres_direct_terminal(
    owner_sessionmaker,
    *,
    task_type: str,
    include_response_payload: bool = True,
) -> SimpleNamespace:
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.chat_transcript import append_session_event
    from app.services.direct_invocation_terminal_boundary_processor import (
        enqueue_direct_terminal_boundary_for_task,
    )

    tenant_id, user_id, parent_agent_id, child_agent_id, parent_session_id, session_id, task_id = (
        uuid.uuid4() for _ in range(7)
    )
    if task_type != "delegation":
        parent_session_id = session_id
    direct_agent_id = child_agent_id if task_type == "delegation" else parent_agent_id
    source = {"business_task": "task", "trigger": "trigger", "delegation": "agent"}[task_type]
    final_response = f"{task_type} completed with canonical evidence."
    response_payload = {
        "agent_id": str(direct_agent_id),
        "session_id": str(session_id),
        "source": source,
        "messages": [{"role": "user", "content": f"Run the {task_type} fixture."}],
        "metadata": {
            "tenant_id": str(tenant_id),
            "final_response": final_response,
        },
    }
    if task_type == "business_task":
        outcome = {
            "status": "succeeded",
            "runtime_status": "completed",
            "reflection_session_id": str(session_id),
            "terminal_reason": "turn_stop",
        }
        if include_response_payload:
            outcome["response_complete_payload"] = response_payload
        task_metadata = {"phase": "terminal", "outcome": outcome}
    else:
        task_metadata = {"terminal_reason": "turn_stop"}
        if include_response_payload:
            task_metadata["response_complete_payload"] = response_payload
        if task_type == "trigger":
            task_metadata.update(
                {
                    "trigger_ids": [str(uuid.uuid4())],
                    "trigger_names": ["PostgreSQL fixture"],
                    "trigger_types": ["cron"],
                }
            )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            Tenant(
                id=tenant_id,
                name=f"Direct terminal {task_type}",
                slug=f"direct-terminal-{task_type}-{tenant_id.hex[:10]}",
            )
        )
        db.add(
            User(
                id=user_id,
                username=f"direct-{user_id.hex[:10]}",
                email=f"direct-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Direct Terminal Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add_all(
            [
                Agent(
                    id=parent_agent_id,
                    tenant_id=tenant_id,
                    name=f"{task_type} parent",
                    creator_id=user_id,
                    owner_user_id=user_id,
                ),
                Agent(
                    id=child_agent_id,
                    tenant_id=tenant_id,
                    name=f"{task_type} child",
                    creator_id=user_id,
                    owner_user_id=user_id,
                ),
            ]
        )
        await db.flush()
        task = RuntimeTask(
            id=task_id,
            tenant_id=tenant_id,
            task_type=task_type,
            parent_agent_id=parent_agent_id,
            child_agent_id=child_agent_id if task_type == "delegation" else None,
            parent_session_id=str(parent_session_id),
            child_session_id=str(session_id),
            root_session_id=str(parent_session_id),
            root_user_id=user_id,
            status="completed",
            completed_at=datetime.now(UTC),
            prompt=f"Run the {task_type} fixture.",
            result_summary=final_response,
            terminal_boundary_generation=1,
            metadata_json=task_metadata,
        )
        db.add(task)
        await db.flush()
        if task_type == "delegation":
            db.add(
                ChatSession(
                    id=parent_session_id,
                    tenant_id=tenant_id,
                    agent_id=parent_agent_id,
                    user_id=user_id,
                    source_channel="web",
                    visibility_scope="team",
                    listed_surface="chat",
                    title="Delegation parent",
                )
            )
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=direct_agent_id,
                user_id=user_id,
                runtime_task_id=task_id,
                source_channel=source,
                runtime_source="runtime_task",
                visibility_scope="agent_owner",
                listed_surface="task_updates",
                title=f"Direct terminal {task_type}",
            )
        )
        await db.flush()
        user_event = await append_session_event(
            db=db,
            tenant_id=tenant_id,
            agent_id=direct_agent_id,
            session_id=session_id,
            run_id=task_id,
            actor_type="user",
            event_type="user_message",
            role="user",
            user_id=user_id,
            content=f"Run the {task_type} fixture.",
            source=source,
            metadata={"turn_id": f"turn-{task_id.hex}"},
            bridge_to_t0=False,
        )
        assistant_event = await append_session_event(
            db=db,
            tenant_id=tenant_id,
            agent_id=direct_agent_id,
            session_id=session_id,
            run_id=task_id,
            actor_type="assistant",
            event_type="assistant_message",
            role="assistant",
            user_id=user_id,
            content=final_response,
            source=source,
            metadata={"turn_id": f"turn-{task_id.hex}"},
            bridge_to_t0=False,
        )
        for event in (user_event.transcript_event, assistant_event.transcript_event):
            event.projection_status = "projected"
            event.projected_at = datetime.now(UTC)
        await db.flush()

        outbox_id = None
        if include_response_payload:
            outbox = await enqueue_direct_terminal_boundary_for_task(db, task)
            assert outbox is not None
            outbox_id = outbox.id
            assert task.terminal_boundary_enqueued_at is not None
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(RuntimeTerminalBoundaryOutbox)
                    .where(RuntimeTerminalBoundaryOutbox.runtime_task_id == task_id)
                )
                == 1
            )
            events = list(
                (
                    await db.execute(
                        select(ChatTranscriptEvent)
                        .where(ChatTranscriptEvent.run_id == task_id)
                        .order_by(ChatTranscriptEvent.sequence)
                    )
                ).scalars()
            )
            assert [(event.actor_type, event.event_type) for event in events] == [
                ("user", "user_message"),
                ("assistant", "assistant_message"),
            ]

    return SimpleNamespace(
        tenant_id=tenant_id,
        user_id=user_id,
        parent_agent_id=parent_agent_id,
        child_agent_id=child_agent_id,
        agent_id=direct_agent_id,
        parent_session_id=parent_session_id,
        session_id=session_id,
        task_id=task_id,
        outbox_id=outbox_id,
        assistant_event_id=assistant_event.event_id,
        assistant_sequence=assistant_event.sequence,
        source=source,
        final_response=final_response,
    )


@pytest.mark.parametrize("task_type", ["business_task", "trigger", "delegation"])
async def test_postgres_direct_terminal_enqueue_claim_validate_process_ack_is_stable_and_tenant_scoped(
    owner_sessionmaker,
    app_user_sessionmaker,
    task_type,
) -> None:
    from app.database import tenant_scoped_session
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.runtime.hooks import HookEvent
    from app.services.direct_invocation_terminal_boundary_processor import (
        DirectInvocationTerminalBoundaryProcessor,
        enqueue_direct_terminal_boundary_for_task,
    )
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seeded = await _seed_postgres_direct_terminal(owner_sessionmaker, task_type=task_type)
    assert seeded.outbox_id is not None

    if task_type == "trigger":
        from app.services.runtime_notification_outbox import CompletionNotification, enqueue_completion_notification

        async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
            await enqueue_completion_notification(
                db,
                CompletionNotification(
                    tenant_id=seeded.tenant_id,
                    source_kind="trigger",
                    source_run_id=str(seeded.task_id),
                    parent_session_id=seeded.session_id,
                    parent_agent_id=seeded.agent_id,
                    parent_user_id=seeded.user_id,
                    terminal_status="completed",
                    task_type="trigger",
                    summary=seeded.final_response,
                    delivery_mode="session_projection",
                    artifacts=[{"path": "runtime_artifacts/triggers/result.json"}],
                ),
            )
            await db.commit()
            task = await db.get(RuntimeTask, seeded.task_id)
            assert task is not None
            assert task.result_summary == seeded.final_response

    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, seeded.task_id)
        assert task is not None
        assert await enqueue_direct_terminal_boundary_for_task(db, task) is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RuntimeTerminalBoundaryOutbox)
                .where(RuntimeTerminalBoundaryOutbox.runtime_task_id == seeded.task_id)
            )
            == 1
        )

    foreign_tenant_id = uuid.uuid4()
    async with tenant_scoped_session(foreign_tenant_id, session_factory=app_user_sessionmaker) as db:
        assert await db.get(RuntimeTerminalBoundaryOutbox, seeded.outbox_id) is None

    order: list[str] = []

    async def bridge_to_t0(*, transcript_event_id, attempts):
        assert transcript_event_id == seeded.assistant_event_id
        assert attempts > 0
        order.append("t0_bridge")
        return True

    async def project_turn(ctx):
        assert ctx.event is HookEvent.TURN_STOP
        assert ctx.agent_id == seeded.agent_id
        assert ctx.session_id == str(seeded.session_id)
        assert ctx.source == seeded.source
        order.append("required_turn_stop")

    async def project_response(ctx):
        assert ctx.event is HookEvent.RESPONSE_COMPLETE
        assert ctx.agent_id == seeded.agent_id
        assert ctx.metadata["final_response"] == seeded.final_response
        order.append("required_response_complete")
        return {"receipt_sha256": "e" * 64}

    async def emit_advisory(event, **kwargs):
        assert kwargs["agent_id"] == seeded.agent_id
        order.append(f"advisory:{event.value}")

    def seal_t0(**kwargs):
        assert kwargs["expected_runtime_task_id"] == seeded.task_id
        assert kwargs["boundary_id"] == seeded.outbox_id
        order.append("seal_t0")
        return SimpleNamespace(
            boundary_id=seeded.outbox_id,
            event_id=f"evt_{seeded.assistant_event_id.hex}",
            sequence=seeded.assistant_sequence,
        )

    processor = DirectInvocationTerminalBoundaryProcessor(
        session_factory=app_user_sessionmaker,
        bridge_to_t0=bridge_to_t0,
        turn_boundary_projector=project_turn,
        emit_advisory_hook=emit_advisory,
        response_projector=project_response,
        seal_t0=seal_t0,
    )
    service = RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker)
    worker_id = f"direct-pg-{task_type}"
    claimed = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id=worker_id,
        task_types=(task_type,),
    )
    assert len(claimed) == 1
    item = claimed[0]
    assert item.id == seeded.outbox_id
    assert item.event_kind == "turn_stop"
    assert item.agent_id == seeded.agent_id
    if task_type == "delegation":
        assert item.agent_id == seeded.child_agent_id
        assert item.agent_id != seeded.parent_agent_id

    assert await service.process_terminal_boundary(
        item=item,
        worker_id=worker_id,
        canonical_validator=processor.validate,
        process_callback=processor,
    )

    expected_custom = {
        "business_task": [],
        "trigger": ["advisory:trigger_end"],
        "delegation": ["advisory:delegation_end"],
    }[task_type]
    assert order == [
        "t0_bridge",
        "required_turn_stop",
        "seal_t0",
        "advisory:turn_stop",
        "required_response_complete",
        "advisory:response_complete",
        *expected_custom,
    ]
    assert (
        await service.claim_batch(
            tenant_id=seeded.tenant_id,
            worker_id=f"{worker_id}-replay",
            task_types=(task_type,),
        )
        == []
    )

    async with tenant_scoped_session(seeded.tenant_id, session_factory=app_user_sessionmaker) as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, seeded.outbox_id)
        assert row is not None
        assert row.status == "delivered"
        assert row.attempt_count == 1
        assert row.agent_id == seeded.agent_id
        assert row.delivery_receipt_json["boundary_id"] == str(seeded.outbox_id)
        assert row.delivery_receipt_json["terminal_event_id"] == str(seeded.assistant_event_id)
        if task_type == "delegation":
            first_projection_sha256 = row.delivery_receipt_json["result_content_sha256"]
            assert len(first_projection_sha256) == 64
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.tenant_id == seeded.tenant_id,
                        ChatTranscriptEvent.agent_id == seeded.parent_agent_id,
                        ChatTranscriptEvent.session_id == seeded.parent_session_id,
                        ChatTranscriptEvent.run_id == seeded.task_id,
                        ChatTranscriptEvent.event_type == "child_session",
                    )
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(RuntimeNotificationOutbox)
                    .where(
                        RuntimeNotificationOutbox.tenant_id == seeded.tenant_id,
                        RuntimeNotificationOutbox.source_kind == "a2a_delegation",
                        RuntimeNotificationOutbox.source_run_id == str(seeded.task_id),
                    )
                )
                == 1
            )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RuntimeTerminalBoundaryOutbox)
                .where(RuntimeTerminalBoundaryOutbox.runtime_task_id == seeded.task_id)
            )
            == 1
        )

    if task_type == "delegation":
        replay_receipt = await processor(replace(item, attempt=2))
        assert replay_receipt["result_content_sha256"] == first_projection_sha256
        async with tenant_scoped_session(seeded.tenant_id, session_factory=app_user_sessionmaker) as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.tenant_id == seeded.tenant_id,
                        ChatTranscriptEvent.agent_id == seeded.parent_agent_id,
                        ChatTranscriptEvent.session_id == seeded.parent_session_id,
                        ChatTranscriptEvent.run_id == seeded.task_id,
                        ChatTranscriptEvent.event_type == "child_session",
                    )
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(RuntimeNotificationOutbox)
                    .where(
                        RuntimeNotificationOutbox.tenant_id == seeded.tenant_id,
                        RuntimeNotificationOutbox.source_kind == "a2a_delegation",
                        RuntimeNotificationOutbox.source_run_id == str(seeded.task_id),
                    )
                )
                == 1
            )


async def test_postgres_completed_direct_terminal_missing_payload_reconcile_is_held(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.direct_invocation_terminal_boundary_processor import (
        enqueue_direct_terminal_boundary_for_task,
    )
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seeded = await _seed_postgres_direct_terminal(
        owner_sessionmaker,
        task_type="business_task",
        include_response_payload=False,
    )

    async def builder(db, task):
        row = await enqueue_direct_terminal_boundary_for_task(db, task)
        return (row,) if row is not None else ()

    service = RuntimeTerminalBoundaryOutboxService(
        session_factory=app_user_sessionmaker,
        reconcile_retry_seconds=0,
    )
    outcome = await service.reconcile_terminal_tasks_once(
        tenant_id=seeded.tenant_id,
        builder=builder,
        task_types=("business_task",),
    )

    assert outcome == {"claimed": 1, "enqueued": 0, "held": 1}
    async with tenant_scoped_session(seeded.tenant_id, session_factory=app_user_sessionmaker) as db:
        task = await db.get(RuntimeTask, seeded.task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.terminal_boundary_enqueued_at is None
        assert task.terminal_boundary_reconcile_attempt_count == 1
        assert task.terminal_boundary_reconcile_last_error == "DirectInvocationTerminalBoundaryPending"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RuntimeTerminalBoundaryOutbox)
                .where(RuntimeTerminalBoundaryOutbox.runtime_task_id == seeded.task_id)
            )
            == 0
        )


async def test_postgres_trigger_binding_covers_effectful_projection_metadata(owner_sessionmaker) -> None:
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.services.direct_invocation_terminal_boundary_processor import (
        build_direct_terminal_boundary_binding,
    )
    from app.services.trigger_artifacts import trigger_output_artifact_ref

    seeded = await _seed_postgres_direct_terminal(owner_sessionmaker, task_type="trigger")
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, seeded.task_id)
        assert task is not None
        original = await build_direct_terminal_boundary_binding(db, task)
        metadata = dict(task.metadata_json or {})
        metadata.update(
            {
                "trigger_ids": [str(uuid.uuid4())],
                "trigger_names": ["Mutated trigger"],
                "trigger_types": ["webhook"],
                "trigger_settlement": {"schema": "trigger_runtime_settlement.v1", "hold": True},
                "output_artifact": trigger_output_artifact_ref(str(task.id)),
                "trigger_artifact_input": {
                    "triggers": [{"id": str(uuid.uuid4()), "name": "mutated", "type": "webhook"}],
                    "metadata": {"execution_class": "interactive_automation"},
                },
            }
        )
        task.metadata_json = metadata
        await db.flush()
        mutated = await build_direct_terminal_boundary_binding(db, task)

    assert mutated["authority_sha256"] != original["authority_sha256"]


@pytest.mark.asyncio
async def test_postgres_direct_binding_covers_hook_turn_identity(owner_sessionmaker) -> None:
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.services.direct_invocation_terminal_boundary_processor import (
        build_direct_terminal_boundary_binding,
    )

    seeded = await _seed_postgres_direct_terminal(owner_sessionmaker, task_type="trigger")
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, seeded.task_id)
        assert task is not None
        original = await build_direct_terminal_boundary_binding(db, task)
        metadata = dict(task.metadata_json or {})
        metadata["turn_id"] = "mutated-hook-turn"
        task.metadata_json = metadata
        await db.flush()
        mutated = await build_direct_terminal_boundary_binding(db, task)

    assert mutated["authority_sha256"] != original["authority_sha256"]


async def test_adopted_idle_sealed_t0_boundary_returns_canonical_receipt(tmp_path) -> None:
    """The direct twin must expose the outbox UUID, not the idle evt_ boundary."""

    from app.memory.t0.ledger import append_t0_session_event, seal_t0_session_segment
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_type="assistant_final.completed",
        role="assistant",
        content="done",
        runtime_task_id=item.runtime_task_id,
        metadata={"turn_id": material.turn_id},
        data_root=tmp_path,
    )
    idle_seal = seal_t0_session_segment(
        agent_id=item.agent_id,
        session_id=item.session_id,
        reason="session_idle",
        data_root=tmp_path,
    )
    assert idle_seal is not None
    assert str(idle_seal.boundary_id or "").startswith("evt_")

    async def no_hook(_ctx):
        return None

    async def no_advisory(*_args, **_kwargs):
        return None

    processor = DirectInvocationTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=no_hook,
        emit_advisory_hook=no_advisory,
        data_root=tmp_path,
    )
    seal = await processor._seal_turn(item, material)
    assert seal.boundary_id == str(item.id)
    assert seal.event_id == idle_seal.event_id
    assert seal.sequence == idle_seal.sequence
