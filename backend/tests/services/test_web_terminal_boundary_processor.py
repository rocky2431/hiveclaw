from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
import uuid

import pytest


pytestmark = pytest.mark.asyncio


def _claimed(*, event_kind: str = "turn_stop", terminal_status: str = "completed"):
    from app.services.runtime_terminal_boundary_outbox import ClaimedTerminalBoundary

    tenant_id, task_id, agent_id, session_id, boundary_id, event_id = (uuid.uuid4() for _ in range(6))
    authority_id = uuid.uuid4() if event_kind == "turn_stop" else task_id
    return ClaimedTerminalBoundary(
        id=boundary_id,
        tenant_id=tenant_id,
        runtime_task_id=task_id,
        agent_id=agent_id,
        session_id=str(session_id),
        event_kind=event_kind,
        terminal_status=terminal_status,
        authority_ref="session_run_outcome" if event_kind == "turn_stop" else "runtime_task",
        authority_id=str(authority_id),
        binding={
            "tenant_id": str(tenant_id),
            "runtime_task_id": str(task_id),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "authority_ref": "session_run_outcome" if event_kind == "turn_stop" else "runtime_task",
            "authority_id": str(authority_id),
            "terminal_event_id": str(event_id),
            "terminal_sequence": 17,
            "authority_sha256": "a" * 64,
        },
        binding_sha256="b" * 64,
        idempotency_key="c" * 64,
        claim_token=uuid.uuid4(),
        attempt=1,
    )


def _material(item, *, sequence: int = 17):
    from app.services.web_terminal_boundary_processor import _WebTerminalMaterial

    final_response = "Use the committed answer."
    response_commit = None
    response_messages: tuple[dict[str, Any], ...] = ()
    summary_messages: tuple[dict[str, Any], ...] = ()
    if item.event_kind == "turn_stop":
        response_messages = ({"role": "user", "content": "Use pnpm."},)
        summary_messages = (*response_messages, {"role": "assistant", "content": final_response})
        response_commit = {
            "schema": "hive.response_commit.v1",
            "committed": True,
            "commit_kind": "session_v2_terminal_outcome",
            "idempotency_key": f"session-run-outcome:{item.authority_id}",
            "source_refs": [f"session-run-outcome://{item.authority_id}"],
        }
    return _WebTerminalMaterial(
        tenant_id=item.tenant_id,
        runtime_task_id=item.runtime_task_id,
        agent_id=item.agent_id,
        session_id=uuid.UUID(item.session_id),
        turn_id=f"turn-{item.runtime_task_id.hex}",
        event_kind=item.event_kind,
        terminal_status=item.terminal_status,
        terminal_event_id=uuid.UUID(str(item.binding["terminal_event_id"])),
        terminal_sequence=sequence,
        agent_name="Fixture Agent",
        user_id=uuid.uuid4(),
        response_messages=response_messages,
        summary_messages=summary_messages,
        response_commit=response_commit,
        main_provider="openai",
        main_model="gpt-fixture",
        source_refs=(f"runtime-task://{item.runtime_task_id}",),
    )


async def test_canonical_request_reconstruction_uses_redacted_snapshot_and_not_raw_response() -> None:
    from app.services.web_terminal_boundary_processor import _request_messages

    active_secret = "sk-live-secret-that-must-not-return"
    snapshot = {
        "wire_request": {
            "messages": [
                {"role": "system", "content": "governed system prompt"},
                {"role": "user", "content": "token=<redacted:tool-config>"},
                {"role": "system", "content": "authorized runtime reminder"},
            ]
        },
        "untrusted_response_copy": {"content": active_secret},
    }

    messages = _request_messages(snapshot)

    assert messages == [
        {"role": "user", "content": "token=<redacted:tool-config>"},
    ]
    assert active_secret not in repr(messages)


async def test_required_turn_boundary_awaits_activation_then_strict_t0_t2(monkeypatch) -> None:
    from app.runtime import hooks_setup
    from app.runtime.hooks import HookContext, HookEvent

    order: list[str] = []

    async def activation(_ctx):
        order.append("activation_summary")

    async def t0_stop(_ctx, *, required_t2=False):
        assert required_t2 is True
        order.append("t0_t2")

    monkeypatch.setattr(hooks_setup, "_summarize_activation_feedback_on_turn_stop", activation)
    monkeypatch.setattr(hooks_setup, "_t0_turn_stop", t0_stop)
    ctx = HookContext(
        event=HookEvent.TURN_STOP,
        agent_id=uuid.uuid4(),
        session_id=str(uuid.uuid4()),
        source="web",
        metadata={
            "terminal_boundary_id": str(uuid.uuid4()),
            "terminal_boundary_idempotency_key": "a" * 64,
            "runtime_task_id": str(uuid.uuid4()),
            "turn_id": f"turn-{uuid.uuid4().hex}",
        },
    )

    await hooks_setup.project_required_turn_boundary(ctx)

    assert order == ["activation_summary", "t0_t2"]


async def test_required_t2_enqueue_failure_propagates(monkeypatch, tmp_path) -> None:
    from app.memory.t2 import segment_package
    from app.runtime import hooks_setup
    from app.runtime.hooks import HookContext, HookEvent
    from app.services import tenant_resolver

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    async def resolve(_agent_id):
        assert _agent_id == agent_id
        return tenant_id

    def fail_enqueue(**_kwargs):
        raise RuntimeError("durable T2 enqueue failed")

    monkeypatch.setattr(hooks_setup, "_t0_segment_t2_eligible", lambda _ctx: (True, "eligible"))
    monkeypatch.setattr(hooks_setup, "_agent_data_root", lambda: tmp_path)
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", resolve)
    monkeypatch.setattr(segment_package, "enqueue_t2_segment_package_job", fail_enqueue)
    ctx = HookContext(
        event=HookEvent.TURN_STOP,
        agent_id=agent_id,
        session_id=str(uuid.uuid4()),
        source="web",
        metadata={"tenant_id": str(tenant_id), "semantic_memory_eligible": True},
    )

    with pytest.raises(RuntimeError, match="durable T2 enqueue failed"):
        await hooks_setup._build_t2_for_sealed_segment(
            ctx=ctx,
            agent_id=agent_id,
            segment_id="segment-required",
            required=True,
        )


async def test_required_t2_rejects_persisted_failed_job(monkeypatch, tmp_path) -> None:
    from app.memory.t2 import segment_package
    from app.runtime import hooks_setup
    from app.runtime.hooks import HookContext, HookEvent
    from app.services import tenant_resolver

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(hooks_setup, "_t0_segment_t2_eligible", lambda _ctx: (True, "eligible"))
    monkeypatch.setattr(hooks_setup, "_agent_data_root", lambda: tmp_path)
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", lambda _agent_id: _async_value(tenant_id))
    monkeypatch.setattr(
        segment_package,
        "enqueue_t2_segment_package_job",
        lambda **_kwargs: SimpleNamespace(
            status="failed",
            job_id="job-failed",
            package_dir=tmp_path / "failed-package",
        ),
    )
    ctx = HookContext(
        event=HookEvent.TURN_STOP,
        agent_id=agent_id,
        session_id=str(uuid.uuid4()),
        source="web",
        metadata={"tenant_id": str(tenant_id), "semantic_memory_eligible": True},
    )

    with pytest.raises(RuntimeError, match="not durably accepted: failed"):
        await hooks_setup._build_t2_for_sealed_segment(
            ctx=ctx,
            agent_id=agent_id,
            segment_id="segment-required-failed",
            required=True,
        )


async def test_success_processor_orders_projection_seal_learning_and_summary(monkeypatch) -> None:
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    order: list[str] = []

    async def bridge(**kwargs):
        order.append("t0_catchup")
        assert kwargs["transcript_event_id"] == material.terminal_event_id
        return True

    async def project_turn(ctx):
        order.append("turn_stop")
        assert ctx.event.value == "turn_stop"
        assert ctx.metadata["terminal_boundary_id"] == str(item.id)

    async def response(ctx):
        order.append("response_complete")
        assert ctx.messages == list(material.response_messages)
        assert ctx.metadata["response_commit"] == material.response_commit
        return {
            "source_refs": [f"session-run-outcome://{item.authority_id}"],
            "receipt_sha256": "d" * 64,
        }

    async def advisory(event, **kwargs):
        order.append(f"advisory_{event.value}")
        if event.value == "turn_stop":
            assert kwargs["metadata"]["required_terminal_boundary_projected"] is True
        else:
            assert event.value == "response_complete"
            assert kwargs["metadata"]["required_response_complete_projected"] is True
        assert kwargs["metadata"]["terminal_boundary_idempotency_key"] == item.idempotency_key

    def seal(**kwargs):
        order.append("seal_receipt")
        assert kwargs["boundary_id"] == item.id
        return SimpleNamespace(
            boundary_id=str(item.id),
            event_id=f"evt_{'1' * 32}",
            sequence=33,
        )

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=bridge,
        turn_boundary_projector=project_turn,
        emit_advisory_hook=advisory,
        response_projector=response,
        seal_t0=seal,
    )

    async def load(_item):
        return material

    async def verify(_material):
        order.append("t0_verified")

    async def summary(_material):
        order.append("summary_cas")
        return material.terminal_sequence, f"chat-session-summary://{material.session_id}/{material.terminal_sequence}"

    monkeypatch.setattr(processor, "_load", load)
    monkeypatch.setattr(processor, "_verify_t0_frontier", verify)
    monkeypatch.setattr(processor, "_project_summary", summary)

    receipt = await processor(item)

    assert order == [
        "t0_catchup",
        "t0_verified",
        "turn_stop",
        "seal_receipt",
        "advisory_turn_stop",
        "response_complete",
        "advisory_response_complete",
        "summary_cas",
    ]
    assert receipt["terminal_sequence"] == material.terminal_sequence
    assert receipt["summary_sequence"] == material.terminal_sequence
    assert len(receipt["response_projection_sha256"]) == 64
    assert f"runtime-terminal-boundary://{item.id}" in receipt["source_refs"]
    assert all("/hook/" not in ref and "/response-complete" not in ref for ref in receipt["source_refs"])


async def test_pending_t0_catchup_prevents_seal_learning_summary_and_ack(monkeypatch) -> None:
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryPending, WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(False),
        turn_boundary_projector=lambda _ctx: pytest.fail("TURN_STOP must wait for T0 catch-up"),
        response_projector=lambda _ctx: pytest.fail("RESPONSE_COMPLETE must wait for T0 catch-up"),
        seal_t0=lambda **_kwargs: pytest.fail("T0 seal must wait for T0 catch-up"),
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(
        processor,
        "_verify_t0_frontier",
        lambda _material: pytest.fail("frontier verification must wait for bridge acceptance"),
    )

    with pytest.raises(WebTerminalBoundaryPending, match="T0 projection is pending"):
        await processor(item)


async def test_required_projection_then_registered_turn_stop_plugin_runs_once() -> None:
    from app.runtime.hooks import HookContext, HookEvent, HookRegistry
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    registry = HookRegistry()
    required_calls: list[str] = []
    plugin_calls: list[tuple[str, str]] = []

    async def project_turn(ctx):
        required_calls.append(ctx.event.value)

    async def governed_plugin(ctx):
        assert ctx.metadata["required_terminal_boundary_projected"] is True
        plugin_calls.append(
            (
                str(ctx.metadata["terminal_boundary_id"]),
                str(ctx.metadata["terminal_boundary_idempotency_key"]),
            )
        )

    registry.register(
        HookEvent.TURN_STOP,
        governed_plugin,
        matcher=lambda ctx: str(ctx.agent_id) == str(item.agent_id),
        key="plugin:fixture:turn-stop",
        handler_name="governed_hook_runner",
        failure_mode="advisory",
    )

    async def emit_registered(event, **kwargs):
        kwargs.pop("evidence_mode")
        return await registry.emit(HookContext(event=event, **kwargs))

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=project_turn,
        emit_advisory_hook=emit_registered,
        seal_t0=lambda **_kwargs: SimpleNamespace(
            boundary_id=str(item.id),
            event_id=f"evt_{'5' * 32}",
            sequence=34,
        ),
    )

    await processor._seal_turn(item=item, material=material)

    assert required_calls == ["turn_stop"]
    assert plugin_calls == [(str(item.id), item.idempotency_key)]


async def test_required_turn_projection_failure_prevents_seal_learning_summary_and_ack(monkeypatch) -> None:
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    order: list[str] = []

    async def bridge(**_kwargs):
        order.append("t0_catchup")
        return True

    async def project_turn(_ctx):
        order.append("required_turn_projection")
        raise RuntimeError("durable T2 enqueue failed")

    async def forbidden_response(_ctx):
        raise AssertionError("RESPONSE_COMPLETE must wait for required TURN_STOP projection")

    def forbidden_seal(**_kwargs):
        raise AssertionError("seal verification must wait for required TURN_STOP projection")

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=bridge,
        turn_boundary_projector=project_turn,
        response_projector=forbidden_response,
        seal_t0=forbidden_seal,
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(
        processor,
        "_verify_t0_frontier",
        lambda _material: _async_value(order.append("t0_verified")),
    )
    monkeypatch.setattr(
        processor,
        "_project_summary",
        lambda _material: pytest.fail("summary must wait for required TURN_STOP projection"),
    )

    with pytest.raises(RuntimeError, match="durable T2 enqueue failed"):
        await processor(item)
    assert order == ["t0_catchup", "t0_verified", "required_turn_projection"]


async def test_response_projection_failure_prevents_summary_and_ack_receipt(monkeypatch) -> None:
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    summary_calls: list[int] = []

    async def bridge(**_kwargs):
        return True

    async def project_turn(_ctx):
        return None

    async def response(_ctx):
        raise RuntimeError("durable learning write failed")

    async def no_advisory(*_args, **_kwargs):
        return None

    def seal(**_kwargs):
        return SimpleNamespace(boundary_id=str(item.id), event_id=f"evt_{'2' * 32}", sequence=33)

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=bridge,
        turn_boundary_projector=project_turn,
        emit_advisory_hook=no_advisory,
        response_projector=response,
        seal_t0=seal,
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))

    async def summary(_material):
        summary_calls.append(1)
        return material.terminal_sequence, "chat-session-summary://unreachable"

    monkeypatch.setattr(processor, "_project_summary", summary)

    with pytest.raises(RuntimeError, match="durable learning write failed"):
        await processor(item)
    assert summary_calls == []


async def test_ack_gap_replay_does_not_rerun_or_rewrite_session_projection(monkeypatch, tmp_path) -> None:
    from app.runtime import hooks_setup
    from app.services.agent_asset_transaction import read_agent_asset_revision
    from app.services.session_memory import SessionMemoryPayload
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    build_calls: list[int] = []
    summary_attempts = 0

    async def build_payload(_messages, metadata, **_kwargs):
        build_calls.append(1)
        return SessionMemoryPayload(
            session_id=str(metadata["session_id"]),
            source=str(metadata["source"]),
            current_state="Committed response projection.",
            task_spec="Survive an outbox ack gap without rewriting continuity.",
        )

    async def no_learning_brain(**_kwargs):
        return None

    async def skipped_candidate(**_kwargs):
        return {"status": "skipped", "reason": "low_signal"}

    async def bridge(**_kwargs):
        return True

    async def project_turn(_ctx):
        return None

    advisory_attempts: list[tuple[str, str]] = []

    async def advisory_replay(event, **kwargs):
        advisory_attempts.append((event.value, str(kwargs["metadata"]["terminal_boundary_idempotency_key"])))

    def seal(**_kwargs):
        return SimpleNamespace(boundary_id=str(item.id), event_id=f"evt_{'3' * 32}", sequence=33)

    monkeypatch.setattr(hooks_setup, "_agent_data_root", lambda: tmp_path)
    monkeypatch.setattr(hooks_setup, "build_session_memory_payload_with_llm", build_payload)
    monkeypatch.setattr(hooks_setup, "_run_fast_reflection_learning_brain", no_learning_brain)
    monkeypatch.setattr(hooks_setup, "_create_fast_reflection_candidate", skipped_candidate)
    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=bridge,
        turn_boundary_projector=project_turn,
        emit_advisory_hook=advisory_replay,
        seal_t0=seal,
        data_root=tmp_path,
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))
    response_receipts: list[tuple[str, tuple[str, ...]]] = []
    project_response = processor._project_response

    async def capture_response_receipt(response_item, response_material):
        receipt = await project_response(response_item, response_material)
        response_receipts.append(receipt)
        return receipt

    monkeypatch.setattr(processor, "_project_response", capture_response_receipt)

    async def summary(_material):
        nonlocal summary_attempts
        summary_attempts += 1
        if summary_attempts == 1:
            raise RuntimeError("crash after consumers before outbox ack")
        return material.terminal_sequence, f"chat-session-summary://{material.session_id}/{material.terminal_sequence}"

    monkeypatch.setattr(processor, "_project_summary", summary)

    with pytest.raises(RuntimeError, match="crash after consumers before outbox ack"):
        await processor(item)

    projection_path = (
        tmp_path / str(material.agent_id) / "memory" / "session_state" / str(material.session_id) / "session_memory.md"
    )
    first_content = projection_path.read_bytes()
    first_mtime = projection_path.stat().st_mtime_ns
    first_revision = read_agent_asset_revision(tmp_path / str(material.agent_id))

    receipt = await processor(item)

    assert receipt["summary_sequence"] == material.terminal_sequence
    assert build_calls == [1]
    assert summary_attempts == 2
    assert projection_path.read_bytes() == first_content
    assert projection_path.stat().st_mtime_ns == first_mtime
    assert read_agent_asset_revision(tmp_path / str(material.agent_id)) == first_revision
    assert advisory_attempts == [
        ("turn_stop", item.idempotency_key),
        ("response_complete", item.idempotency_key),
        ("turn_stop", item.idempotency_key),
        ("response_complete", item.idempotency_key),
    ]
    assert response_receipts[0] == response_receipts[1]


async def _async_value(value):
    return value


async def test_turn_abort_only_catches_up_and_seals(monkeypatch) -> None:
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed(event_kind="turn_abort", terminal_status="failed")
    material = _material(item)
    order: list[str] = []

    async def bridge(**_kwargs):
        order.append("t0_catchup")
        return True

    async def project_turn(ctx):
        order.append(ctx.event.value)

    async def advisory(event, **kwargs):
        order.append("advisory_turn_abort")
        assert event.value == "turn_abort"
        assert kwargs["metadata"]["required_terminal_boundary_projected"] is True

    async def forbidden_response(_ctx):
        raise AssertionError("turn_abort must not run RESPONSE_COMPLETE")

    async def forbidden_summary(_material):
        raise AssertionError("turn_abort must not run summary")

    def seal(**_kwargs):
        order.append("seal")
        return SimpleNamespace(boundary_id=str(item.id), event_id=f"evt_{'4' * 32}", sequence=23)

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=bridge,
        turn_boundary_projector=project_turn,
        emit_advisory_hook=advisory,
        response_projector=forbidden_response,
        seal_t0=seal,
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(order.append("verified")))
    monkeypatch.setattr(processor, "_project_summary", forbidden_summary)

    receipt = await processor(item)

    assert order == ["t0_catchup", "verified", "turn_abort", "seal", "advisory_turn_abort"]
    assert "response_projection_sha256" not in receipt
    assert "summary_sequence" not in receipt


async def test_completed_runtime_task_stop_seals_without_response_or_summary(monkeypatch) -> None:
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    original = _claimed()
    item = replace(
        original,
        authority_ref="runtime_task",
        authority_id=str(original.runtime_task_id),
        binding={
            **dict(original.binding),
            "authority_ref": "runtime_task",
            "authority_id": str(original.runtime_task_id),
        },
    )
    material = replace(
        _material(item),
        response_messages=(),
        summary_messages=(),
        response_commit=None,
        main_provider="",
        main_model="",
    )
    order: list[str] = []

    async def project_turn(ctx):
        order.append(ctx.event.value)

    async def advisory(event, **_kwargs):
        order.append(f"advisory_{event.value}")

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(order.append("t0_catchup") or True),
        turn_boundary_projector=project_turn,
        emit_advisory_hook=advisory,
        response_projector=lambda _ctx: pytest.fail("RuntimeTask stop must not project RESPONSE_COMPLETE"),
        seal_t0=lambda **_kwargs: SimpleNamespace(
            boundary_id=str(item.id),
            event_id=f"evt_{'6' * 32}",
            sequence=24,
        ),
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(order.append("verified")))
    monkeypatch.setattr(
        processor,
        "_project_summary",
        lambda _material: pytest.fail("RuntimeTask stop must not project a semantic summary"),
    )

    receipt = await processor(item)

    assert order == ["t0_catchup", "verified", "turn_stop", "advisory_turn_stop"]
    assert "response_projection_sha256" not in receipt
    assert "summary_sequence" not in receipt


@pytest.mark.parametrize(
    ("status", "outcome_state", "event_kind", "authority_ref"),
    (
        ("completed", "terminal_committed", "turn_stop", "session_run_outcome"),
        ("completed", None, "turn_stop", "runtime_task"),
        ("failed", None, "turn_abort", "runtime_task"),
    ),
)
async def test_enqueue_web_terminal_boundary_for_task_selects_one_authority(
    monkeypatch,
    status,
    outcome_state,
    event_kind,
    authority_ref,
) -> None:
    from app.services import web_terminal_boundary_processor as processor_module

    task_id, tenant_id, agent_id, session_id, outcome_id = (uuid.uuid4() for _ in range(5))
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        status=status,
        terminal_boundary_generation=1,
        terminal_boundary_enqueued_at=None,
    )
    outcome = SimpleNamespace(id=outcome_id, state=outcome_state) if outcome_state else None
    captured: dict[str, Any] = {}

    class FakeDB:
        async def scalar(self, _statement):
            return outcome

    async def build(_db, **kwargs):
        captured["build"] = kwargs
        return {"authority_id": str(kwargs["authority_id"])}

    async def enqueue(_db, **kwargs):
        captured["enqueue"] = kwargs
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(processor_module, "build_web_terminal_boundary_binding", build)
    monkeypatch.setattr("app.services.runtime_terminal_boundary_outbox.enqueue_terminal_boundary", enqueue)

    row = await processor_module.enqueue_web_terminal_boundary_for_task(FakeDB(), task)

    selected_authority_id = outcome_id if outcome_state else task_id
    assert row is not None
    assert captured["build"]["event_kind"] == event_kind
    assert captured["build"]["authority_ref"] == authority_ref
    assert captured["build"]["authority_id"] == selected_authority_id
    assert captured["enqueue"]["binding"] == {"authority_id": str(selected_authority_id)}


@pytest.mark.parametrize("terminal_status", ("completed", "failed"))
async def test_enqueue_web_terminal_boundary_for_task_holds_noncommitted_outcome_marker(terminal_status) -> None:
    from app.services.web_terminal_boundary_processor import (
        WebTerminalBoundaryPending,
        enqueue_web_terminal_boundary_for_task,
    )

    task_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        tenant_id=uuid.uuid4(),
        parent_agent_id=uuid.uuid4(),
        parent_session_id=str(uuid.uuid4()),
        status=terminal_status,
        terminal_boundary_generation=1,
        terminal_boundary_enqueued_at=None,
    )

    class FakeDB:
        async def scalar(self, _statement):
            return SimpleNamespace(id=uuid.uuid4(), state="prepared")

    with pytest.raises(WebTerminalBoundaryPending, match="not terminal_committed"):
        await enqueue_web_terminal_boundary_for_task(FakeDB(), task)


async def test_enqueue_web_terminal_boundary_for_task_noops_outside_cutover_or_after_enqueue(monkeypatch) -> None:
    from app.services import web_terminal_boundary_processor as processor_module

    class NoDB:
        async def scalar(self, _statement):
            raise AssertionError("no authority query is allowed for a no-op task")

    monkeypatch.setattr(
        processor_module,
        "build_web_terminal_boundary_binding",
        lambda *_args, **_kwargs: pytest.fail("no binding is allowed for a no-op task"),
    )
    base = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "parent_agent_id": uuid.uuid4(),
        "parent_session_id": str(uuid.uuid4()),
        "status": "completed",
    }

    assert (
        await processor_module.enqueue_web_terminal_boundary_for_task(
            NoDB(),
            SimpleNamespace(**base, terminal_boundary_generation=None, terminal_boundary_enqueued_at=None),
        )
        is None
    )
    assert (
        await processor_module.enqueue_web_terminal_boundary_for_task(
            NoDB(),
            SimpleNamespace(
                **base,
                terminal_boundary_generation=1,
                terminal_boundary_enqueued_at=datetime.now(UTC),
            ),
        )
        is None
    )


async def _seed_summary_session(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id, task_id = (uuid.uuid4() for _ in range(5))
    timestamp = datetime(2026, 8, 31, 1, 2, tzinfo=UTC)
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Summary CAS", slug=f"summary-cas-{tenant_id.hex[:10]}"))
        db.add(
            User(
                id=user_id,
                username=f"summary-{user_id.hex[:10]}",
                email=f"summary-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Summary Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Summary Agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_session_id=str(session_id),
                root_user_id=user_id,
                status="completed",
                completed_at=datetime.now(UTC),
                prompt="summary fixture",
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                runtime_task_id=task_id,
                title="Summary fixture",
                summary="newer canonical summary",
                summary_through_sequence=20,
                last_message_at=timestamp,
            )
        )
        await db.commit()
    return SimpleNamespace(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
        timestamp=timestamp,
    )


async def test_summary_cas_never_overwrites_newer_projection_or_message_order(owner_sessionmaker) -> None:
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor, _WebTerminalMaterial

    seeded = await _seed_summary_session(owner_sessionmaker)
    generated: list[int] = []

    async def generate(*_args, **_kwargs):
        generated.append(1)
        return "stale summary"

    processor = WebTerminalBoundaryProcessor(
        session_factory=owner_sessionmaker,
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        summary_generator=generate,
    )
    material = _WebTerminalMaterial(
        tenant_id=seeded.tenant_id,
        runtime_task_id=seeded.task_id,
        agent_id=seeded.agent_id,
        session_id=seeded.session_id,
        turn_id=f"turn-{seeded.task_id.hex}",
        event_kind="turn_stop",
        terminal_status="completed",
        terminal_event_id=uuid.uuid4(),
        terminal_sequence=19,
        agent_name="Summary Agent",
        user_id=seeded.user_id,
        response_messages=({"role": "user", "content": "old"},),
        summary_messages=(
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
        ),
        response_commit={},
        main_provider="openai",
        main_model="gpt-fixture",
        source_refs=(),
    )

    sequence, source_ref = await processor._project_summary(material)

    equal_sequence, equal_source_ref = await processor._project_summary(replace(material, terminal_sequence=20))

    async with owner_sessionmaker() as db:
        session = await db.get(
            __import__("app.models.chat_session", fromlist=["ChatSession"]).ChatSession, seeded.session_id
        )
    assert generated == []
    assert sequence == 20 and source_ref.endswith("/20")
    assert equal_sequence == 20
    assert equal_source_ref == f"chat-session-summary://{seeded.session_id}/20"
    assert session is not None
    assert session.summary == "newer canonical summary"
    assert session.summary_through_sequence == 20
    assert session.last_message_at == seeded.timestamp


@pytest.mark.parametrize("unavailable_summary", [None, " \x00 "])
async def test_summary_unavailable_is_typed_retry_without_mechanical_fallback(
    owner_sessionmaker,
    unavailable_summary,
) -> None:
    from app.models.chat_session import ChatSession
    from app.services.web_terminal_boundary_processor import (
        WebTerminalBoundaryPending,
        WebTerminalBoundaryProcessor,
        _WebTerminalMaterial,
    )

    seeded = await _seed_summary_session(owner_sessionmaker)

    generated: list[int] = []

    async def generate(*_args, **_kwargs):
        generated.append(1)
        return unavailable_summary if len(generated) == 1 else "recovered LLM summary"

    processor = WebTerminalBoundaryProcessor(
        session_factory=owner_sessionmaker,
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        summary_generator=generate,
    )
    material = _WebTerminalMaterial(
        tenant_id=seeded.tenant_id,
        runtime_task_id=seeded.task_id,
        agent_id=seeded.agent_id,
        session_id=seeded.session_id,
        turn_id=f"turn-{seeded.task_id.hex}",
        event_kind="turn_stop",
        terminal_status="completed",
        terminal_event_id=uuid.uuid4(),
        terminal_sequence=21,
        agent_name="Summary Agent",
        user_id=seeded.user_id,
        response_messages=({"role": "user", "content": "new"},),
        summary_messages=(
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "new answer"},
        ),
        response_commit={},
        main_provider="openai",
        main_model="gpt-fixture",
        source_refs=(),
    )

    with pytest.raises(WebTerminalBoundaryPending, match="summary provider returned no semantic result"):
        await processor._project_summary(material)
    sequence, source_ref = await processor._project_summary(material)

    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, seeded.session_id)
    assert sequence == 21
    assert source_ref == f"chat-session-summary://{seeded.session_id}/21"
    assert generated == [1, 1]
    assert session is not None
    assert session.summary == "recovered LLM summary"
    assert session.summary_through_sequence == 21
    assert (session.transcript_metadata_json or {})["terminal_summary_projection"]["state"] == "sealed"
    assert session.last_message_at == seeded.timestamp


async def test_summary_provider_exception_is_typed_hold_without_reinvocation(owner_sessionmaker) -> None:
    from app.models.chat_session import ChatSession
    from app.services.web_terminal_boundary_processor import (
        WebTerminalBoundaryPending,
        WebTerminalBoundaryProcessor,
        _WebTerminalMaterial,
    )

    seeded = await _seed_summary_session(owner_sessionmaker)
    provider_calls = 0

    async def generate(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise TimeoutError("provider outcome is ambiguous")

    processor = WebTerminalBoundaryProcessor(
        session_factory=owner_sessionmaker,
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        summary_generator=generate,
    )
    material = _WebTerminalMaterial(
        tenant_id=seeded.tenant_id,
        runtime_task_id=seeded.task_id,
        agent_id=seeded.agent_id,
        session_id=seeded.session_id,
        turn_id=f"turn-{seeded.task_id.hex}",
        event_kind="turn_stop",
        terminal_status="completed",
        terminal_event_id=uuid.uuid4(),
        terminal_sequence=21,
        agent_name="Summary Agent",
        user_id=seeded.user_id,
        response_messages=({"role": "user", "content": "new"},),
        summary_messages=(
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "new answer"},
        ),
        response_commit={},
        main_provider="openai",
        main_model="gpt-fixture",
        source_refs=(),
    )

    with pytest.raises(WebTerminalBoundaryPending, match="summary provider outcome is unknown"):
        await processor._project_summary(material)
    with pytest.raises(WebTerminalBoundaryPending, match="summary provider outcome is unknown"):
        await processor._project_summary(material)

    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, seeded.session_id)
    assert provider_calls == 1
    assert session is not None
    assert session.summary == "newer canonical summary"
    assert session.summary_through_sequence == 20
    projection = (session.transcript_metadata_json or {})["terminal_summary_projection"]
    assert projection["state"] == "needs_reconciliation"
    assert projection["error_code"] == "TimeoutError"


async def test_summary_provider_ack_gap_holds_without_reinvoking_provider(owner_sessionmaker) -> None:
    from app.services.web_terminal_boundary_processor import (
        WebTerminalBoundaryPending,
        WebTerminalBoundaryProcessor,
        _WebTerminalMaterial,
    )

    seeded = await _seed_summary_session(owner_sessionmaker)
    provider_calls = 0

    async def generate(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return "provider result that must not be regenerated"

    class CrashBeforeSealSessionFactory:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("crash after provider result before durable summary seal")
            return owner_sessionmaker()

    material = _WebTerminalMaterial(
        tenant_id=seeded.tenant_id,
        runtime_task_id=seeded.task_id,
        agent_id=seeded.agent_id,
        session_id=seeded.session_id,
        turn_id=f"turn-{seeded.task_id.hex}",
        event_kind="turn_stop",
        terminal_status="completed",
        terminal_event_id=uuid.uuid4(),
        terminal_sequence=21,
        agent_name="Summary Agent",
        user_id=seeded.user_id,
        response_messages=({"role": "user", "content": "new"},),
        summary_messages=(
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "new answer"},
        ),
        response_commit={},
        main_provider="openai",
        main_model="gpt-fixture",
        source_refs=(),
    )
    crashing = WebTerminalBoundaryProcessor(
        session_factory=CrashBeforeSealSessionFactory(),
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        summary_generator=generate,
    )

    with pytest.raises(RuntimeError, match="crash after provider result"):
        await crashing._project_summary(material)

    replay = WebTerminalBoundaryProcessor(
        session_factory=owner_sessionmaker,
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        summary_generator=generate,
    )
    with pytest.raises(WebTerminalBoundaryPending, match="summary provider outcome is unknown"):
        await replay._project_summary(material)

    async with owner_sessionmaker() as db:
        session = await db.get(
            __import__("app.models.chat_session", fromlist=["ChatSession"]).ChatSession,
            seeded.session_id,
        )
    assert provider_calls == 1
    assert session is not None
    assert session.summary == "newer canonical summary"
    assert session.summary_through_sequence == 20
    assert (session.transcript_metadata_json or {})["terminal_summary_projection"]["state"] == "in_flight"


async def test_stable_t0_boundary_replay_does_not_seal_a_new_turn(tmp_path) -> None:
    from app.memory.t0.ledger import append_t0_session_event
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_type="assistant_final.completed",
        role="assistant",
        content="first answer",
        runtime_task_id=item.runtime_task_id,
        metadata={"turn_id": material.turn_id},
        data_root=tmp_path,
    )

    async def no_hook(_ctx):
        return None

    async def no_advisory(*_args, **_kwargs):
        return None

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=no_hook,
        emit_advisory_hook=no_advisory,
        data_root=tmp_path,
    )
    first = await processor._seal_turn(item=item, material=material)

    next_task_id = uuid.uuid4()
    next_turn_id = f"turn-{next_task_id.hex}"
    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_type="user_prompt.accepted",
        role="user",
        content="next prompt",
        runtime_task_id=next_task_id,
        metadata={"turn_id": next_turn_id},
        data_root=tmp_path,
    )
    replay = await processor._seal_turn(item=item, material=material)
    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=item.session_id,
        event_type="assistant_text.delta",
        role="assistant",
        content="still open",
        runtime_task_id=next_task_id,
        metadata={"turn_id": next_turn_id},
        data_root=tmp_path,
    )

    assert replay.segment_id == first.segment_id
    assert replay.event_id == first.event_id


async def test_committed_outcome_binding_and_material_are_canonical_redacted_rows(owner_sessionmaker) -> None:
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.runtime_terminal_boundary_outbox import (
        ClaimedTerminalBoundary,
        TerminalBoundaryCanonicalMismatch,
        terminal_boundary_binding_sha256,
    )
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events
    from app.services.web_terminal_boundary_processor import (
        _load_terminal_material,
        _sha256,
        build_web_terminal_boundary_binding,
        validate_web_terminal_boundary,
    )

    tenant_id, user_id, agent_id, session_id, task_id, result_id, outcome_id = (uuid.uuid4() for _ in range(7))
    assistant_event_id = uuid.uuid4()
    active_secret = "sk-live-secret-that-was-redacted-before-prepare"
    request_snapshot = {
        "provider": "openai",
        "model": "gpt-fixture",
        "wire_request": {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "token=<redacted:tool-config>"},
            ]
        },
    }
    final_response = "Committed redacted answer."
    model_request_sha256 = _sha256(request_snapshot)
    semantic_content_sha256 = _sha256(final_response)
    result_content_sha256 = _sha256([])
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Canonical Web", slug=f"canonical-web-{tenant_id.hex[:10]}"))
        db.add(
            User(
                id=user_id,
                username=f"canonical-{user_id.hex[:10]}",
                email=f"canonical-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Canonical Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Canonical Agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_session_id=str(session_id),
                root_user_id=user_id,
                status="completed",
                completed_at=datetime.now(UTC),
                prompt="canonical fixture",
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                runtime_task_id=task_id,
                title="Canonical fixture",
            )
        )
        await db.flush()
        db.add(
            SessionModelResult(
                id=result_id,
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=f"turn-{task_id.hex}",
                run_id=task_id,
                round_id=f"{task_id}:round:1",
                provider_request_id=f"hive:{task_id}:round:1:attempt:1",
                state="round_committed",
                model_request_hash=model_request_sha256,
                model_request_snapshot_json=request_snapshot,
                bound_input_ids_json=[],
                seal_json={
                    "continuation": {"verdict": "terminal_candidate"},
                    "logical_round_complete": True,
                    "semantic_content": final_response,
                    "content_hash": semantic_content_sha256,
                    "block_ledger": [],
                },
            )
        )
        await db.flush()
        assistant_event = (
            await append_session_events(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        event_id=assistant_event_id,
                        item_id=uuid.uuid4(),
                        item_kind="assistant_final",
                        lifecycle="completed",
                        scope={
                            "level": "round",
                            "session_id": str(session_id),
                            "thread_id": str(session_id),
                            "turn_id": f"turn-{task_id.hex}",
                            "run_id": str(task_id),
                            "round_id": f"{task_id}:round:1",
                        },
                        actor={"type": "assistant", "agent_id": str(agent_id)},
                        payload={
                            "zero_copy": True,
                            "outcome_id": str(outcome_id),
                            "terminal_result_id": str(result_id),
                            "render_owner_id": str(uuid.uuid4()),
                            "source_blocks": [],
                            "result_content_hash": result_content_sha256,
                            "artifact_refs": [],
                            "parts": [],
                        },
                        result_id=result_id,
                        content_hash=result_content_sha256,
                    )
                ],
            )
        )[0]
        db.add(
            SessionRunOutcome(
                id=outcome_id,
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=f"turn-{task_id.hex}",
                run_id=task_id,
                terminal_result_id=result_id,
                state="terminal_committed",
                eligibility_snapshot_hash="4" * 64,
                seal_json={
                    "result_content_hash": result_content_sha256,
                    "semantic_content_hash": semantic_content_sha256,
                },
                terminal_event_id=assistant_event.id,
            )
        )
        await db.flush()
        terminal_event = (
            await append_session_events(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=outcome_id,
                        item_kind="run_outcome",
                        lifecycle="terminal_committed",
                        scope={
                            "level": "run",
                            "session_id": str(session_id),
                            "thread_id": str(session_id),
                            "turn_id": f"turn-{task_id.hex}",
                            "run_id": str(task_id),
                        },
                        actor={"type": "runtime"},
                        payload={"outcome_id": str(outcome_id), "terminal_result_id": str(result_id)},
                        content_hash="6" * 64,
                    )
                ],
            )
        )[0]
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        binding = await build_web_terminal_boundary_binding(
            db,
            tenant_id=tenant_id,
            runtime_task_id=task_id,
            agent_id=agent_id,
            session_id=str(session_id),
            event_kind="turn_stop",
            terminal_status="completed",
            authority_ref="session_run_outcome",
            authority_id=outcome_id,
        )
        item = ClaimedTerminalBoundary(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            runtime_task_id=task_id,
            agent_id=agent_id,
            session_id=str(session_id),
            event_kind="turn_stop",
            terminal_status="completed",
            authority_ref="session_run_outcome",
            authority_id=str(outcome_id),
            binding=binding,
            binding_sha256=terminal_boundary_binding_sha256(binding),
            idempotency_key="7" * 64,
            claim_token=uuid.uuid4(),
            attempt=1,
        )
        for event_id, sequence in (
            (assistant_event.id, assistant_event.sequence),
            (terminal_event.id, terminal_event.sequence),
        ):
            projected_event = await db.get(ChatTranscriptEvent, event_id)
            assert projected_event is not None
            projected_event.metadata_json = {
                **dict(projected_event.metadata_json or {}),
                "t0_bridge_pending": False,
                "t0_bridge_event_id": f"evt_{event_id.hex}",
                "t0_bridge_sequence": int(sequence),
                "t0_bridge_relay_source": "runtime_control_bus",
            }
        await db.flush()
        assert await validate_web_terminal_boundary(db, item) == binding
        material = await _load_terminal_material(db, item)

    assert binding["terminal_event_id"] == str(terminal_event.id)
    assert binding["assistant_final_event_id"] == str(assistant_event.id)
    assert binding["terminal_sequence"] > binding["assistant_final_sequence"]
    assert binding["model_request_sha256"] == model_request_sha256
    assert binding["semantic_content_sha256"] == semantic_content_sha256
    assert material.response_messages == ({"role": "user", "content": "token=<redacted:tool-config>"},)
    assert material.summary_messages[-1] == {"role": "assistant", "content": "Committed redacted answer."}
    assert active_secret not in repr(material)

    async with owner_sessionmaker() as db:
        result = await db.get(SessionModelResult, result_id)
        assert result is not None
        result.model_request_snapshot_json = {**request_snapshot, "model": "tampered-model"}
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(TerminalBoundaryCanonicalMismatch, match="request snapshot hash mismatch"):
            await validate_web_terminal_boundary(db, item)

    async with owner_sessionmaker() as db:
        result = await db.get(SessionModelResult, result_id)
        assert result is not None
        result.model_request_snapshot_json = request_snapshot
        result.seal_json = {**dict(result.seal_json or {}), "semantic_content": "Tampered final response."}
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(TerminalBoundaryCanonicalMismatch, match="semantic content hash mismatch"):
            await _load_terminal_material(db, item)


async def test_completed_runtime_task_binding_pins_frontier_and_sanitized_hook_lineage(owner_sessionmaker) -> None:
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.runtime_terminal_boundary_outbox import (
        ClaimedTerminalBoundary,
        TerminalBoundaryCanonicalMismatch,
        terminal_boundary_binding_sha256,
    )
    from app.services.web_terminal_boundary_processor import (
        _load_terminal_material,
        _sha256,
        build_web_terminal_boundary_binding,
    )

    tenant_id, user_id, agent_id, session_id, task_id, event_id = (uuid.uuid4() for _ in range(6))
    source_session_id, anchor_event_id, regenerate_source_id, intent_id = (uuid.uuid4() for _ in range(4))
    secret_prose = "do not bind this prompt or activation reason"
    activation_event_id = "ae:" + "1" * 24
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Runtime boundary", slug=f"runtime-boundary-{tenant_id.hex[:10]}"))
        db.add(
            User(
                id=user_id,
                username=f"runtime-boundary-{user_id.hex[:10]}",
                email=f"runtime-boundary-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Runtime Boundary Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Runtime Boundary Agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=source_session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Runtime boundary source",
            )
        )
        await db.flush()
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="web_chat_turn",
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                root_session_id=str(source_session_id),
                root_runtime_task_id=task_id,
                root_user_id=user_id,
                status="completed",
                completed_at=datetime.now(UTC),
                prompt=secret_prose,
                result_summary=secret_prose,
                metadata_json={
                    "turn_id": f"turn-{task_id.hex}",
                    "intent_id": str(intent_id),
                    "branch_mode": "regenerate",
                    "source_session_id": str(source_session_id),
                    "branch_session_id": str(session_id),
                    "anchor_event_id": str(anchor_event_id),
                    "regenerate_from_event_id": str(anchor_event_id),
                    "regenerate_prompt_source_event_id": str(regenerate_source_id),
                    "regenerate_prompt": secret_prose,
                    "runtime_assembly_state": {
                        "activation_events": [
                            {
                                "event_id": activation_event_id,
                                "event_type": "tool_success",
                                "intent_id": str(intent_id),
                                "query_id": "query:fixture-1",
                                "candidate_id": "candidate:fixture-1",
                                "feedback": {"credit": 0.75, "reason": secret_prose},
                                "metadata": {"secret": secret_prose},
                            }
                        ]
                    },
                },
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                runtime_task_id=task_id,
                parent_session_id=source_session_id,
                root_session_id=source_session_id,
                title="Runtime boundary fixture",
                transcript_metadata_json={
                    "branch_mode": "regenerate",
                    "source_session_id": str(source_session_id),
                    "anchor_event_id": str(anchor_event_id),
                    "anchor_sequence": 4,
                },
            )
        )
        await db.flush()
        db.add(
            ChatTranscriptEvent(
                id=event_id,
                sequence=5,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=task_id,
                actor_type="runtime",
                event_type="tool_result",
                content=secret_prose,
                content_hash=None,
                turn_id=f"turn-{task_id.hex}",
                projection_status="projected",
            )
        )
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        binding = await build_web_terminal_boundary_binding(
            db,
            tenant_id=tenant_id,
            runtime_task_id=task_id,
            agent_id=agent_id,
            session_id=str(session_id),
            event_kind="turn_stop",
            terminal_status="completed",
            authority_ref="runtime_task",
            authority_id=task_id,
        )
        item = ClaimedTerminalBoundary(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            runtime_task_id=task_id,
            agent_id=agent_id,
            session_id=str(session_id),
            event_kind="turn_stop",
            terminal_status="completed",
            authority_ref="runtime_task",
            authority_id=str(task_id),
            binding=binding,
            binding_sha256=terminal_boundary_binding_sha256(binding),
            idempotency_key="8" * 64,
            claim_token=uuid.uuid4(),
            attempt=1,
        )
        material = await _load_terminal_material(db, item)

    assert binding["terminal_event_id"] == str(event_id)
    assert binding["terminal_sequence"] == 5
    assert len(binding["terminal_event_sha256"]) == 64
    assert any(ref.get("runtime_task_id") == str(task_id) for ref in binding["source_refs"])
    assert material.response_commit is None
    assert material.hook_metadata["branch_mode"] == "regenerate"
    assert material.hook_metadata["root_session_id"] == str(source_session_id)
    assert material.hook_metadata["parent_session_id"] == str(source_session_id)
    assert material.hook_metadata["branch_session_id"] == str(session_id)
    assert material.hook_metadata["anchor_event_id"] == str(anchor_event_id)
    assert material.hook_metadata["regenerate_from_event_id"] == str(anchor_event_id)
    assert material.hook_metadata["regenerate_prompt_source_event_id"] == str(regenerate_source_id)
    assert material.hook_metadata["intent_id"] == str(intent_id)
    assert material.hook_metadata["runtime_assembly_state"]["activation_events"] == [
        {
            "event_id": activation_event_id,
            "event_type": "tool_success",
            "intent_id": str(intent_id),
            "query_id": "query:fixture-1",
            "candidate_id": "candidate:fixture-1",
            "feedback": {"credit": 0.75},
        }
    ]
    assert secret_prose not in repr(binding)
    assert secret_prose not in repr(material.hook_metadata)

    newer_event_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            ChatTranscriptEvent(
                id=newer_event_id,
                sequence=6,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=task_id,
                actor_type="runtime",
                event_type="tool_card",
                content="later canonical frontier",
                content_hash=None,
                turn_id=f"turn-{task_id.hex}",
                projection_status="projected",
            )
        )
        await db.commit()
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(TerminalBoundaryCanonicalMismatch, match="no longer matches"):
            await _load_terminal_material(db, item)

    result_id, outcome_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            SessionModelResult(
                id=result_id,
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=f"turn-{task_id.hex}",
                run_id=task_id,
                round_id=f"{task_id}:round:marker",
                provider_request_id=f"hive:{task_id}:round:marker",
                state="prepared",
                model_request_hash=_sha256({}),
                model_request_snapshot_json={},
                bound_input_ids_json=[],
            )
        )
        await db.flush()
        db.add(
            SessionRunOutcome(
                id=outcome_id,
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=f"turn-{task_id.hex}",
                run_id=task_id,
                terminal_result_id=result_id,
                state="prepared",
                eligibility_snapshot_hash="9" * 64,
            )
        )
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(TerminalBoundaryCanonicalMismatch, match="SessionRunOutcome marker"):
            await build_web_terminal_boundary_binding(
                db,
                tenant_id=tenant_id,
                runtime_task_id=task_id,
                agent_id=agent_id,
                session_id=str(session_id),
                event_kind="turn_stop",
                terminal_status="completed",
                authority_ref="runtime_task",
                authority_id=task_id,
            )


async def test_adopted_idle_sealed_t0_boundary_returns_canonical_receipt(monkeypatch, tmp_path) -> None:
    """The canonical redrive of an idle-sealed turn returns a canonical receipt.

    The idle seal leaves the real boundary event (``evt_<hex>`` identity) in
    place; the adopted receipt must expose the caller-proven outbox UUID as
    ``t0_boundary_id`` so the strict binding normalizer accepts it instead of
    dead-lettering after projection.  The segment reproduces the verified
    production bridge shape: exact RuntimeTask identity with no stored turn
    anywhere in the segment or its events.
    """
    from app.memory.t0.ledger import append_t0_session_event, seal_t0_session_segment
    from app.services.runtime_terminal_boundary_outbox import normalize_terminal_boundary_binding
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=uuid.UUID(item.session_id),
        event_type="assistant_final.completed",
        role="assistant",
        content="final answer",
        runtime_task_id=item.runtime_task_id,
        data_root=tmp_path,
    )
    idle_seal = seal_t0_session_segment(
        agent_id=item.agent_id,
        session_id=uuid.UUID(item.session_id),
        reason="session_idle",
        data_root=tmp_path,
    )
    assert idle_seal is not None
    assert str(idle_seal.boundary_id or "").startswith("evt_")

    async def no_hook(_ctx):
        return None

    async def no_advisory(*_args, **_kwargs):
        return None

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        turn_boundary_projector=no_hook,
        emit_advisory_hook=no_advisory,
        data_root=tmp_path,
    )

    async def load_material(_item):
        return material

    async def verify_frontier(_material):
        return None

    async def project_response(_item, _material):
        return ("a" * 64, (f"session-model-result://{item.authority_id}",))

    async def project_summary(_material):
        return (
            material.terminal_sequence,
            f"chat-session-summary://{item.session_id}/{material.terminal_sequence}",
        )

    monkeypatch.setattr(processor, "_load", load_material)
    monkeypatch.setattr(processor, "_verify_t0_frontier", verify_frontier)
    monkeypatch.setattr(processor, "_project_response", project_response)
    monkeypatch.setattr(processor, "_project_summary", project_summary)

    receipt = dict(await processor(item))
    normalized = normalize_terminal_boundary_binding(receipt)
    assert normalized["t0_boundary_id"] == str(item.id)
    assert receipt["t0_event_id"] == idle_seal.event_id
    assert receipt["t0_sequence"] == idle_seal.sequence
    assert receipt["t0_boundary_id"] != idle_seal.boundary_id


def _seed_idle_sealed_t0_with_t2_manifest(tmp_path, item, material, *, manifest_status: str):
    """Reproduce the verified production shape: a no-turn idle-sealed T0
    segment plus a pre-existing stable T2 job manifest in ``manifest_status``.

    Returns the idle seal receipt and the absolute manifest path.  The manifest
    is rewritten to the requested durable status before the canonical redrive,
    mirroring a job that already reached that state.
    """
    import json

    from app.memory.t0.ledger import append_t0_session_event, seal_t0_session_segment
    from app.memory.t2.segment_package import enqueue_t2_segment_package_job

    append_t0_session_event(
        agent_id=item.agent_id,
        session_id=uuid.UUID(item.session_id),
        event_type="assistant_final.completed",
        role="assistant",
        content="final answer",
        runtime_task_id=item.runtime_task_id,
        data_root=tmp_path,
    )
    idle_seal = seal_t0_session_segment(
        agent_id=item.agent_id,
        session_id=uuid.UUID(item.session_id),
        reason="session_idle",
        data_root=tmp_path,
    )
    assert idle_seal is not None
    assert str(idle_seal.boundary_id or "").startswith("evt_")
    receipt = enqueue_t2_segment_package_job(
        data_root=tmp_path,
        agent_id=item.agent_id,
        tenant_id=item.tenant_id,
        session_id=uuid.UUID(item.session_id),
        t0_segment_id=idle_seal.segment_id,
    )
    manifest_path = receipt.staging_dir / "job_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = manifest_status
    manifest["package_status"] = manifest_status
    manifest["issues"] = [f"fixture_{manifest_status}"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return idle_seal, manifest_path


def _local_required_projection_patches(monkeypatch, tmp_path, item):
    from app.services import tenant_resolver

    async def resolve(agent_id):
        assert agent_id == item.agent_id
        return item.tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", resolve)
    monkeypatch.setattr("app.runtime.hooks_setup._agent_data_root", lambda: tmp_path)
    monkeypatch.setattr(
        "app.memory.t0.ledger.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )


async def test_required_projection_adopts_idle_sealed_segment_with_held_t2_manifest(monkeypatch, tmp_path) -> None:
    """The real required TURN_STOP projection settles an idle-sealed no-turn segment.

    This runs the production ``project_required_turn_boundary`` path (not a
    ``no_hook`` stub): the required projector adopts the idle-sealed segment,
    the required T2 gate accepts the pre-existing stable ``held`` manifest
    without rewriting it, the full processor returns the canonical UUID
    receipt with the real idle boundary event/sequence, and T0 index/events
    stay byte-identical.
    """
    from app.services.runtime_terminal_boundary_outbox import normalize_terminal_boundary_binding
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    idle_seal, manifest_path = _seed_idle_sealed_t0_with_t2_manifest(tmp_path, item, material, manifest_status="held")
    session_root = tmp_path / str(item.agent_id) / "memory" / "t0" / "sessions" / item.session_id
    index_before = (session_root / "index.json").read_bytes()
    events_before = idle_seal.jsonl_path.read_bytes()
    manifest_before = manifest_path.read_bytes()
    _local_required_projection_patches(monkeypatch, tmp_path, item)

    async def no_advisory(*_args, **_kwargs):
        return None

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        emit_advisory_hook=no_advisory,
        data_root=tmp_path,
    )
    monkeypatch.setattr(processor, "_load", lambda _item: _async_value(material))
    monkeypatch.setattr(processor, "_verify_t0_frontier", lambda _material: _async_value(None))
    monkeypatch.setattr(processor, "_project_response", lambda _item, _material: _async_value(("a" * 64, ())))
    monkeypatch.setattr(
        processor,
        "_project_summary",
        lambda _material: _async_value(
            (material.terminal_sequence, f"chat-session-summary://{item.session_id}/{material.terminal_sequence}")
        ),
    )

    receipt = dict(await processor(item))

    normalized = normalize_terminal_boundary_binding(receipt)
    assert normalized["t0_boundary_id"] == str(item.id)
    assert receipt["t0_event_id"] == idle_seal.event_id
    assert receipt["t0_sequence"] == idle_seal.sequence
    assert receipt["t0_boundary_id"] != idle_seal.boundary_id
    assert (session_root / "index.json").read_bytes() == index_before
    assert idle_seal.jsonl_path.read_bytes() == events_before
    assert manifest_path.read_bytes() == manifest_before, "the held T2 manifest must be preserved verbatim"


async def test_required_t2_gate_rejects_preexisting_failed_manifest(monkeypatch, tmp_path) -> None:
    """A pre-existing ``failed`` T2 manifest is not durable acceptance.

    The required gate must keep failing the terminal settlement closed instead
    of silently treating the failed manifest as success or rebuilding it inside
    terminal settlement; the manifest stays byte-identical for later repair.
    """
    import json

    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    item = _claimed()
    material = _material(item)
    idle_seal, manifest_path = _seed_idle_sealed_t0_with_t2_manifest(tmp_path, item, material, manifest_status="failed")
    manifest_before = manifest_path.read_bytes()
    _local_required_projection_patches(monkeypatch, tmp_path, item)

    async def no_advisory(*_args, **_kwargs):
        return None

    processor = WebTerminalBoundaryProcessor(
        bridge_to_t0=lambda **_kwargs: _async_value(True),
        emit_advisory_hook=no_advisory,
        data_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="not durably accepted: failed"):
        await processor._seal_turn(item=item, material=material)

    assert manifest_path.read_bytes() == manifest_before
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    # The failed gate must not have sealed a canonical terminal boundary on top
    # of the idle seal either.
    assert idle_seal.jsonl_path.read_text(encoding="utf-8").count('"segment_boundary"') == 1
