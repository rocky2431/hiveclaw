from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, func, select


pytestmark = pytest.mark.asyncio


async def _seed_terminal_task(
    owner_sessionmaker,
    *,
    tenant_name: str,
    task_type: str = "business_task",
) -> SimpleNamespace:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id, task_id = (uuid.uuid4() for _ in range(5))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=tenant_name, slug=f"terminal-{tenant_id.hex[:12]}"))
        db.add(
            User(
                id=user_id,
                username=f"terminal-{user_id.hex[:12]}",
                email=f"terminal-{user_id.hex[:12]}@test.local",
                password_hash="x",
                display_name="Terminal Boundary Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name=f"{tenant_name} Agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.flush()
        task = RuntimeTask(
            id=task_id,
            tenant_id=tenant_id,
            task_type=task_type,
            parent_agent_id=agent_id,
            parent_session_id=str(session_id),
            root_session_id=str(session_id),
            root_user_id=user_id,
            status="completed",
            completed_at=datetime.now(UTC),
            prompt="terminal boundary fixture",
            result_summary="fixture completed",
        )
        db.add(task)
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                runtime_task_id=task_id,
                title="Terminal boundary fixture",
            )
        )
        await db.commit()
    return SimpleNamespace(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        task_id=task_id,
    )


def _binding(*, event_id: uuid.UUID | None = None, sequence: int = 7) -> dict:
    terminal_event_id = event_id or uuid.uuid4()
    return {
        "terminal_event_id": str(terminal_event_id),
        "terminal_sequence": sequence,
        "authority_sha256": "a" * 64,
        "source_refs": [
            {
                "event_id": str(terminal_event_id),
                "sequence": sequence,
                "sha256": "b" * 64,
            }
        ],
    }


async def _enqueue(owner_sessionmaker, seeded, *, event_kind: str, binding: dict):
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary

    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, seeded.task_id)
        assert task is not None
        return await enqueue_terminal_boundary(
            db,
            task=task,
            event_kind=event_kind,
            agent_id=seeded.agent_id,
            session_id=seeded.session_id,
            terminal_status="completed",
            authority_ref="runtime_task",
            authority_id=seeded.task_id,
            binding=binding,
        )


async def test_binding_rejects_narrative_or_secret_ingress() -> None:
    from app.services.runtime_terminal_boundary_outbox import (
        TerminalBoundaryBindingError,
        normalize_terminal_boundary_binding,
        terminal_boundary_idempotency_key,
    )

    assert normalize_terminal_boundary_binding(_binding())["terminal_sequence"] == 7
    with pytest.raises(TerminalBoundaryBindingError, match="response_text"):
        normalize_terminal_boundary_binding({"response_text": "model-authored body"})
    with pytest.raises(TerminalBoundaryBindingError, match="content"):
        normalize_terminal_boundary_binding({"source_refs": [{"content": "secret"}]})
    with pytest.raises(TerminalBoundaryBindingError, match="sha256"):
        normalize_terminal_boundary_binding({"authority_sha256": "not-a-sha"})
    for secret_shaped_binding in (
        {"secret_id": "sk-live-do-not-persist"},
        {"payload_id": "entire-model-answer-without-spaces"},
        {"source_ref": "apikey:super-secret-value"},
    ):
        with pytest.raises(TerminalBoundaryBindingError):
            normalize_terminal_boundary_binding(secret_shaped_binding)

    tenant_id = uuid.uuid4()
    authority_id = uuid.uuid4()
    assert terminal_boundary_idempotency_key(
        tenant_id=tenant_id,
        event_kind="turn_stop",
        authority_ref="runtime_task",
        authority_id=authority_id,
    ) == terminal_boundary_idempotency_key(
        tenant_id=tenant_id,
        event_kind="turn_stop",
        authority_ref="runtime_task",
        authority_id=str(authority_id).upper(),
    )


async def test_enqueue_is_stable_idempotent_conflict_safe_and_transactional(owner_sessionmaker) -> None:
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_terminal_boundary_outbox import (
        TerminalBoundaryIdempotencyConflict,
        enqueue_terminal_boundary,
    )

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Stable")
    event_id = uuid.uuid4()
    first = await _enqueue(
        owner_sessionmaker,
        seeded,
        event_kind="turn_stop",
        binding=_binding(event_id=event_id),
    )
    replay = await _enqueue(
        owner_sessionmaker,
        seeded,
        event_kind="turn_stop",
        binding=_binding(event_id=event_id),
    )
    assert replay.id == first.id
    assert replay.idempotency_key == first.idempotency_key

    with pytest.raises(TerminalBoundaryIdempotencyConflict):
        await _enqueue(
            owner_sessionmaker,
            seeded,
            event_kind="turn_stop",
            binding=_binding(event_id=event_id, sequence=8),
        )

    with pytest.raises(RuntimeError, match="rollback fixture"):
        async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
            task = await db.get(RuntimeTask, seeded.task_id)
            assert task is not None
            await enqueue_terminal_boundary(
                db,
                task=task,
                event_kind="response_complete",
                agent_id=seeded.agent_id,
                session_id=seeded.session_id,
                terminal_status="completed",
                authority_ref="session_run_outcome",
                authority_id=uuid.uuid4(),
                binding=_binding(),
            )
            raise RuntimeError("rollback fixture")

    async with owner_sessionmaker() as db:
        rows = list(
            (
                await db.execute(
                    select(RuntimeTerminalBoundaryOutbox).where(
                        RuntimeTerminalBoundaryOutbox.tenant_id == seeded.tenant_id
                    )
                )
            ).scalars()
        )
        task = await db.get(RuntimeTask, seeded.task_id)
    assert [row.event_kind for row in rows] == ["turn_stop"]
    assert task is not None and task.terminal_boundary_enqueued_at is not None


async def test_enqueue_accepts_a_safe_synthetic_session_without_chat_session_row(owner_sessionmaker) -> None:
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_terminal_boundary_outbox import enqueue_terminal_boundary

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Synthetic session")
    synthetic_session_id = f"subagent-{seeded.task_id.hex}-worker-d1"
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, seeded.task_id)
        assert task is not None
        row = await enqueue_terminal_boundary(
            db,
            task=task,
            event_kind="turn_stop",
            agent_id=seeded.agent_id,
            session_id=synthetic_session_id,
            terminal_status="completed",
            authority_ref="runtime_task",
            authority_id=seeded.task_id,
            binding=_binding(),
        )

    assert row.session_id == synthetic_session_id
    assert row.binding_json["session_id"] == synthetic_session_id


async def test_worker_task_type_filter_never_claims_a_sibling_lane(owner_sessionmaker) -> None:
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_terminal_boundary_outbox import (
        RuntimeTerminalBoundaryOutboxService,
        enqueue_terminal_boundary,
    )

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Filtered")
    business = await _enqueue(
        owner_sessionmaker,
        seeded,
        event_kind="turn_stop",
        binding=_binding(sequence=1),
    )
    web_task_id = uuid.uuid4()
    web_session_id = uuid.uuid4()
    async with tenant_scoped_session(seeded.tenant_id, session_factory=owner_sessionmaker) as db:
        web_task = RuntimeTask(
            id=web_task_id,
            tenant_id=seeded.tenant_id,
            task_type="web_chat_turn",
            parent_agent_id=seeded.agent_id,
            parent_session_id=str(web_session_id),
            root_session_id=str(web_session_id),
            root_user_id=seeded.user_id,
            status="completed",
            completed_at=datetime.now(UTC),
            prompt="filtered Web terminal boundary fixture",
        )
        db.add(web_task)
        await db.flush()
        web = await enqueue_terminal_boundary(
            db,
            task=web_task,
            event_kind="turn_stop",
            agent_id=seeded.agent_id,
            session_id=web_session_id,
            terminal_status="completed",
            authority_ref="runtime_task",
            authority_id=web_task_id,
            binding=_binding(sequence=2),
        )
        business_row = await db.get(RuntimeTerminalBoundaryOutbox, business.id)
        assert business_row is not None
        business_row.available_at = datetime.now(UTC) - timedelta(hours=1)

    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    claimed = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id="web-only-worker",
        task_types=("web_chat_turn",),
    )

    assert [item.id for item in claimed] == [web.id]
    async with owner_sessionmaker() as db:
        business_row = await db.get(RuntimeTerminalBoundaryOutbox, business.id)
    assert business_row is not None and business_row.status == "pending"


async def test_worker_validates_canonical_binding_retries_and_acks(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Canonical")
    await _enqueue(owner_sessionmaker, seeded, event_kind="turn_stop", binding=_binding())
    processed: list[uuid.UUID] = []

    async def mismatched(_db, item):
        return {**item.binding, "terminal_sequence": 999}

    async def process(item):
        processed.append(item.id)
        if len(processed) == 1:
            # Simulate a consumer commit followed by an ack-gap exception. The
            # stable outbox ID lets the required consumer deduplicate replay.
            raise RuntimeError("ack gap")
        return {"source_ref": f"runtime-terminal-boundary://{item.id}"}

    service = RuntimeTerminalBoundaryOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=3,
    )
    first = await service.drain_once(
        tenant_id=seeded.tenant_id,
        worker_id="terminal-worker-a",
        canonical_validator=mismatched,
        process_callback=process,
    )
    assert first == {"claimed": 1, "delivered": 0, "retried": 1, "dead_lettered": 0}
    assert processed == []

    async def exact(_db, item):
        return dict(item.binding)

    second = await service.drain_once(
        tenant_id=seeded.tenant_id,
        worker_id="terminal-worker-b",
        canonical_validator=exact,
        process_callback=process,
    )
    assert second == {"claimed": 1, "delivered": 0, "retried": 1, "dead_lettered": 0}
    third = await service.drain_once(
        tenant_id=seeded.tenant_id,
        worker_id="terminal-worker-c",
        canonical_validator=exact,
        process_callback=process,
    )
    assert third == {"claimed": 1, "delivered": 1, "retried": 0, "dead_lettered": 0}
    assert len(processed) == 2
    assert len(set(processed)) == 1
    async with owner_sessionmaker() as db:
        row = await db.scalar(
            select(RuntimeTerminalBoundaryOutbox).where(RuntimeTerminalBoundaryOutbox.tenant_id == seeded.tenant_id)
        )
    assert row is not None and row.status == "delivered" and row.attempt_count == 3
    assert row.delivery_receipt_json == {"source_ref": f"runtime-terminal-boundary://{row.id}"}

    from app.models.chat_session import ChatSession

    outbox_id = row.id
    async with owner_sessionmaker() as db:
        await db.execute(delete(ChatSession).where(ChatSession.id == seeded.session_id))
        await db.commit()
    async with owner_sessionmaker() as db:
        assert await db.get(RuntimeTerminalBoundaryOutbox, outbox_id) is not None
        await db.execute(delete(RuntimeTask).where(RuntimeTask.id == seeded.task_id))
        await db.commit()
    async with owner_sessionmaker() as db:
        assert await db.get(RuntimeTerminalBoundaryOutbox, outbox_id) is None


async def test_worker_reclaims_expired_lease_and_dead_letters_bounded_failure(owner_sessionmaker) -> None:
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Lease")
    await _enqueue(owner_sessionmaker, seeded, event_kind="turn_abort", binding=_binding())
    service = RuntimeTerminalBoundaryOutboxService(
        session_factory=owner_sessionmaker,
        lease_seconds=10,
        retry_base_seconds=0,
        max_attempts=2,
    )
    now = datetime.now(UTC)
    first = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id="crashed-worker",
        now=now,
    )
    before = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id="too-early-worker",
        now=now + timedelta(seconds=9),
    )
    after = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id="recovery-worker",
        now=now + timedelta(seconds=11),
    )
    assert len(first) == 1 and before == [] and len(after) == 1
    assert after[0].id == first[0].id and after[0].attempt == 2
    assert (
        await service.ack_terminal_boundary(
            item=first[0],
            worker_id="crashed-worker",
            receipt={"source_ref": f"runtime-terminal-boundary://{first[0].id}"},
        )
        is False
    )

    outcome = await service.fail_terminal_boundary(
        item=after[0],
        worker_id="recovery-worker",
        error=RuntimeError("permanent consumer failure"),
    )
    assert outcome == "dead_letter"
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, after[0].id)
    assert row is not None and row.status == "dead_letter"
    # Arbitrary callback messages can contain model bytes or secrets; the
    # durable ledger records only the exception type/code.
    assert row.last_error == "RuntimeError"


async def test_dead_letter_redrive_is_exact_tenant_scoped_and_preserves_failure_evidence(owner_sessionmaker) -> None:
    from app.models.audit import AuditLog
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Redrive")
    await _enqueue(owner_sessionmaker, seeded, event_kind="turn_stop", binding=_binding())
    service = RuntimeTerminalBoundaryOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=1,
    )
    claimed = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id="redrive-failing-worker",
    )
    assert len(claimed) == 1
    assert (
        await service.fail_terminal_boundary(
            item=claimed[0],
            worker_id="redrive-failing-worker",
            error=RuntimeError("secret must not enter durable error evidence"),
        )
        == "dead_letter"
    )

    with pytest.raises(LookupError):
        await service.redrive_dead_letter(
            tenant_id=uuid.uuid4(),
            outbox_id=claimed[0].id,
            actor_user_id=seeded.user_id,
            reason="Retry the canonical terminal consumers after operator review.",
        )
    await service.redrive_dead_letter(
        tenant_id=seeded.tenant_id,
        outbox_id=claimed[0].id,
        actor_user_id=seeded.user_id,
        reason="Retry the canonical terminal consumers after operator review.",
    )

    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, claimed[0].id)
        audit = await db.scalar(
            select(AuditLog).where(
                AuditLog.tenant_id == seeded.tenant_id,
                AuditLog.action == "runtime_terminal_boundary_redriven",
            )
        )
    assert row is not None
    assert row.status == "pending"
    assert row.attempt_count == 1
    assert row.last_error == "RuntimeError"
    assert row.claimed_by is None and row.claim_token is None and row.lease_expires_at is None
    assert audit is not None
    assert audit.details["outbox_id"] == str(row.id)
    assert audit.details["previous_attempt_count"] == 1
    assert audit.details["previous_error"] == "RuntimeError"


async def test_redrive_returned_row_survives_route_serialization_after_commit(owner_sessionmaker) -> None:
    """A committed redrive must serialize through the live route without a
    post-commit DetachedInstanceError (HTTP 500 after the effect succeeded).

    The UPDATE expires the server-generated ``updated_at``; the returned row
    must be fully readable once the service session has committed and closed.
    """
    from app.api.runtime_terminal_boundaries import _serialize_terminal_boundary
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Redrive serialize")
    await _enqueue(owner_sessionmaker, seeded, event_kind="turn_stop", binding=_binding())
    service = RuntimeTerminalBoundaryOutboxService(
        session_factory=owner_sessionmaker,
        retry_base_seconds=0,
        max_attempts=1,
    )
    claimed = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id="redrive-serialize-worker",
    )
    assert len(claimed) == 1
    assert (
        await service.fail_terminal_boundary(
            item=claimed[0],
            worker_id="redrive-serialize-worker",
            error=RuntimeError("secret must not enter durable error evidence"),
        )
        == "dead_letter"
    )

    row = await service.redrive_dead_letter(
        tenant_id=seeded.tenant_id,
        outbox_id=claimed[0].id,
        actor_user_id=seeded.user_id,
        reason="Retry the canonical terminal consumers after operator review.",
    )

    item = _serialize_terminal_boundary(row)
    assert item.status == "pending"
    assert item.updated_at is not None


async def test_web_summary_unknown_requires_exact_operator_retry_before_redrive(owner_sessionmaker) -> None:
    from app.models.audit import AuditLog
    from app.models.chat_session import ChatSession
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import web_summary_projection_request_id

    seeded = await _seed_terminal_task(
        owner_sessionmaker,
        tenant_name="Summary redrive",
        task_type="web_chat_turn",
    )
    terminal_sequence = 7
    row = await _enqueue(
        owner_sessionmaker,
        seeded,
        event_kind="turn_stop",
        binding=_binding(sequence=terminal_sequence),
    )
    request_id = web_summary_projection_request_id(
        tenant_id=seeded.tenant_id,
        session_id=seeded.session_id,
        runtime_task_id=seeded.task_id,
        terminal_sequence=terminal_sequence,
    )
    async with owner_sessionmaker() as db:
        persisted = await db.get(RuntimeTerminalBoundaryOutbox, row.id)
        session = await db.get(ChatSession, seeded.session_id)
        assert persisted is not None and session is not None
        persisted.status = "dead_letter"
        persisted.attempt_count = 8
        persisted.last_error = "WebTerminalBoundaryPending"
        session.transcript_metadata_json = {
            "terminal_summary_projection": {
                "schema": "terminal_summary_projection.v1",
                "request_id": request_id,
                "runtime_task_id": str(seeded.task_id),
                "terminal_sequence": terminal_sequence,
                "state": "needs_reconciliation",
                "attempt_count": 1,
            }
        }
        await db.commit()

    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    with pytest.raises(ValueError, match="summary_disposition='retry'"):
        await service.redrive_dead_letter(
            tenant_id=seeded.tenant_id,
            outbox_id=row.id,
            actor_user_id=seeded.user_id,
            reason="Reviewed the unknown provider outcome.",
        )

    await service.redrive_dead_letter(
        tenant_id=seeded.tenant_id,
        outbox_id=row.id,
        actor_user_id=seeded.user_id,
        reason="Reviewed the unknown provider outcome and authorized one retry.",
        summary_disposition="retry",
    )

    async with owner_sessionmaker() as db:
        persisted = await db.get(RuntimeTerminalBoundaryOutbox, row.id)
        session = await db.get(ChatSession, seeded.session_id)
        audit = await db.scalar(
            select(AuditLog).where(
                AuditLog.tenant_id == seeded.tenant_id,
                AuditLog.action == "runtime_terminal_boundary_redriven",
            )
        )
    assert persisted is not None and persisted.status == "pending"
    projection = (session.transcript_metadata_json or {})["terminal_summary_projection"]
    assert projection["state"] == "retryable"
    assert projection["operator_actor_id"] == str(seeded.user_id)
    assert projection["operator_reconciliation_count"] == 1
    assert audit is not None
    assert audit.details["summary_disposition"] == "retry"
    assert audit.details["summary_reconciliation"]["request_id"] == request_id


async def test_slow_processor_renews_exact_claim_without_holding_a_database_lock(owner_sessionmaker) -> None:
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Lease fence")
    await _enqueue(owner_sessionmaker, seeded, event_kind="turn_stop", binding=_binding())
    service = RuntimeTerminalBoundaryOutboxService(
        session_factory=owner_sessionmaker,
        lease_seconds=1,
    )
    claimed = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id="slow-worker",
        limit=100,
    )
    assert len(claimed) == 1

    started = asyncio.Event()
    release = asyncio.Event()
    processor_calls: list[str] = []

    async def exact(_db, item):
        return dict(item.binding)

    async def slow_processor(item):
        processor_calls.append("slow-worker")
        started.set()
        await release.wait()
        return {"source_ref": f"runtime-terminal-boundary://{item.id}"}

    processing = asyncio.create_task(
        service.process_terminal_boundary(
            item=claimed[0],
            worker_id="slow-worker",
            canonical_validator=exact,
            process_callback=slow_processor,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(1.1)
    reclaimed = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id="second-worker",
    )
    assert reclaimed == []
    release.set()
    assert await processing is True
    assert processor_calls == ["slow-worker"]


async def test_lost_terminal_claim_renewal_cancels_the_in_flight_processor(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.services.runtime_terminal_boundary_outbox import (
        RuntimeTerminalBoundaryOutboxService,
        StaleTerminalBoundaryClaim,
    )

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Lost lease fence")
    await _enqueue(owner_sessionmaker, seeded, event_kind="turn_stop", binding=_binding())
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)
    [claimed] = await service.claim_batch(tenant_id=seeded.tenant_id, worker_id="lost-worker")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def exact(_db, item):
        return dict(item.binding)

    async def slow_processor(_item):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def lose_claim(**_kwargs):
        await started.wait()
        raise StaleTerminalBoundaryClaim("lost")

    monkeypatch.setattr(service, "_renew_terminal_boundary_claim_until_cancelled", lose_claim)

    with pytest.raises(StaleTerminalBoundaryClaim, match="lost"):
        await service.process_terminal_boundary(
            item=claimed,
            worker_id="lost-worker",
            canonical_validator=exact,
            process_callback=slow_processor,
        )
    assert cancelled.is_set()


async def test_claim_batch_never_preclaims_work_that_cannot_be_fenced_immediately(owner_sessionmaker) -> None:
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Single claim")
    await _enqueue(owner_sessionmaker, seeded, event_kind="turn_stop", binding=_binding())
    await _enqueue(owner_sessionmaker, seeded, event_kind="turn_abort", binding=_binding())
    service = RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker)

    claimed = await service.claim_batch(
        tenant_id=seeded.tenant_id,
        worker_id="single-claim-worker",
        limit=100,
    )

    assert len(claimed) == 1


async def test_claim_is_tenant_isolated_for_non_owner_role(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    first = await _seed_terminal_task(owner_sessionmaker, tenant_name="Tenant A")
    second = await _seed_terminal_task(owner_sessionmaker, tenant_name="Tenant B")
    await _enqueue(owner_sessionmaker, first, event_kind="turn_stop", binding=_binding())
    await _enqueue(owner_sessionmaker, second, event_kind="turn_stop", binding=_binding())

    service = RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(
        tenant_id=first.tenant_id,
        worker_id="tenant-a-worker",
        limit=10,
    )
    assert len(claimed) == 1
    assert claimed[0].tenant_id == first.tenant_id
    assert claimed[0].tenant_id != second.tenant_id


async def test_terminal_task_reconcile_builder_is_atomic_and_generation_bounded(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.services.runtime_terminal_boundary_outbox import (
        RuntimeTerminalBoundaryOutboxService,
        enqueue_terminal_boundary,
    )

    seeded = await _seed_terminal_task(owner_sessionmaker, tenant_name="Reconcile")
    event_id = uuid.uuid4()

    async def builder(db, task):
        return [
            await enqueue_terminal_boundary(
                db,
                task=task,
                event_kind="turn_stop",
                agent_id=seeded.agent_id,
                session_id=seeded.session_id,
                terminal_status="completed",
                authority_ref="runtime_task",
                authority_id=task.id,
                binding=_binding(event_id=event_id),
            )
        ]

    service = RuntimeTerminalBoundaryOutboxService(
        session_factory=owner_sessionmaker,
        reconcile_retry_seconds=0,
    )
    outcome = await service.reconcile_terminal_tasks_once(
        tenant_id=seeded.tenant_id,
        builder=builder,
    )
    replay = await service.reconcile_terminal_tasks_once(
        tenant_id=seeded.tenant_id,
        builder=builder,
    )
    assert outcome == {"claimed": 1, "enqueued": 1, "held": 0}
    assert replay == {"claimed": 0, "enqueued": 0, "held": 0}
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seeded.task_id)
        count = await db.scalar(
            select(func.count())
            .select_from(RuntimeTerminalBoundaryOutbox)
            .where(RuntimeTerminalBoundaryOutbox.runtime_task_id == seeded.task_id)
        )
    assert task is not None and task.terminal_boundary_enqueued_at is not None
    assert task.terminal_boundary_reconcile_attempt_count == 1
    assert count == 1
