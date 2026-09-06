from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.memory.t0.ledger import (
    EVENT_RECORD_SCHEMA_VERSION,
    EVENTS_FILENAME,
    T0BoundaryTargetMismatch,
    T0SegmentBoundaryPending,
    append_t0_session_event,
    import_legacy_t0_file,
    replay_t0_session_events,
    seal_t0_session_segment,
)


def _append_turn_event(
    *,
    data_root: Path,
    agent_id: UUID,
    session_id: UUID,
    run_id: UUID,
    turn_id: str,
    role: str,
    content: str,
):
    return append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type=f"{role}_message",
        role=role,
        content=content,
        runtime_task_id=run_id,
        source="web",
        metadata={"turn_id": turn_id},
        data_root=data_root,
    )


def test_append_user_and_assistant_messages_to_unified_session_ledger(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()

    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="请按 Claude Code 的 transcript 方式保存这一轮",
        message_id="msg-user-1",
        actor_id="user-1",
        source="web",
        data_root=tmp_path,
        created_at=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
    )
    second = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="assistant_message",
        role="assistant",
        content="已写入 append-only session ledger。",
        message_id="msg-assistant-1",
        actor_id=str(agent_id),
        source="web",
        data_root=tmp_path,
        created_at=datetime(2026, 6, 18, 10, 1, tzinfo=timezone.utc),
    )

    expected_root = tmp_path / str(agent_id) / "memory" / "t0" / "sessions" / str(session_id)
    assert first.path == second.path
    assert first.path == expected_root / "segments" / first.segment_id / "source.md"
    assert first.jsonl_path == expected_root / "segments" / first.segment_id / EVENTS_FILENAME
    assert second.jsonl_path == first.jsonl_path
    assert "logs" not in first.path.parts
    assert first.sequence == 1
    assert second.sequence == 2

    records = [json.loads(line) for line in first.jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert [record["schema_version"] for record in records] == [
        EVENT_RECORD_SCHEMA_VERSION,
        EVENT_RECORD_SCHEMA_VERSION,
    ]
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[0]["content"] == "请按 Claude Code 的 transcript 方式保存这一轮"
    assert records[0]["projection"]["path"] == f"segments/{first.segment_id}/source.md"
    assert records[0]["mechanical_truth"]["format"] == "jsonl"
    assert records[0]["event_hash"]
    assert records[1]["prev_event_hash"] == records[0]["event_hash"]

    content = first.path.read_text(encoding="utf-8")
    assert f"agent_id: {agent_id}" in content
    assert f"session_id: {session_id}" in content
    assert "agent_id: memory" not in content
    assert '<t0_event id="' in content
    assert 'seq="1"' in content
    assert 'event_type="user_message"' in content
    assert "请按 Claude Code" in content
    assert "已写入 append-only session ledger" in content

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.sequence, event.event_type, event.role, event.content) for event in events] == [
        (1, "user_message", "user", "请按 Claude Code 的 transcript 方式保存这一轮"),
        (2, "assistant_message", "assistant", "已写入 append-only session ledger。"),
    ]
    assert [event.record_schema_version for event in events] == [
        EVENT_RECORD_SCHEMA_VERSION,
        EVENT_RECORD_SCHEMA_VERSION,
    ]
    assert [event.truth_path for event in events] == [first.jsonl_path, first.jsonl_path]
    assert [event.path for event in events] == [first.path, first.path]
    assert events[0].event_hash == records[0]["event_hash"]
    assert events[1].prev_event_hash == records[0]["event_hash"]


def test_t0_event_promotes_runtime_metadata_to_mechanical_record_and_projection(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()

    result = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="runtime metadata discipline",
        runtime_task_id="runtime-1",
        source="web_chat",
        metadata={"turn_id": "turn-1", "intent_id": "intent-1", "request_id": "request-1"},
        data_root=tmp_path,
    )

    record = json.loads(result.jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["sequence"] == 1
    assert record["source"] == "web_chat"
    assert record["runtime_task_id"] == "runtime-1"
    assert record["turn_id"] == "turn-1"
    assert record["intent_id"] == "intent-1"

    projection = result.path.read_text(encoding="utf-8")
    assert 'turn_id="turn-1"' in projection
    assert 'intent_id="intent-1"' in projection
    assert 'runtime_task_id="runtime-1"' in projection
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events[0].turn_id == "turn-1"
    assert events[0].intent_id == "intent-1"
    assert events[0].runtime_task_id == "runtime-1"
    assert events[0].source == "web_chat"
    assert events[0].sequence == 1


def test_jsonl_append_uses_o_append_and_single_write(monkeypatch, tmp_path: Path) -> None:
    import app.memory.t0.ledger as ledger

    opened: list[tuple[str, int, int]] = []
    writes: list[bytes] = []
    real_open = os.open

    def fake_open(path, flags, mode=0o777):
        opened.append((str(path), flags, mode))
        return real_open(tmp_path / "actual.jsonl", flags, mode)

    real_write = os.write

    def fake_write(fd, data):
        writes.append(data)
        return real_write(fd, data)

    monkeypatch.setattr(ledger.os, "open", fake_open)
    monkeypatch.setattr(ledger.os, "write", fake_write)

    offset, length = ledger._append_event_record(tmp_path / "events.jsonl", {"sequence": 1, "content": "hello"})

    assert opened
    assert opened[0][1] & os.O_APPEND
    assert opened[0][1] & os.O_CREAT
    assert len(writes) == 1
    assert writes[0].endswith(b"\n")
    assert length == len(writes[0])
    assert offset == 0


def test_replay_falls_back_to_markdown_projection_for_legacy_segments(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()

    result = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="legacy projection still replays",
        data_root=tmp_path,
    )
    result.jsonl_path.unlink()

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)

    assert [(event.sequence, event.event_type, event.content) for event in events] == [
        (1, "user_message", "legacy projection still replays")
    ]
    assert events[0].record_schema_version == "t0.markdown-projection.v1"
    assert events[0].truth_path == result.path


def test_seal_segment_preserves_append_only_history_and_next_turn_gets_new_segment(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()

    first = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="第一段",
        data_root=tmp_path,
    )

    sealed = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="session_idle",
        data_root=tmp_path,
    )
    second = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content="恢复后的第二段",
        data_root=tmp_path,
    )

    assert sealed is not None
    assert sealed.segment_id == first.segment_id
    assert second.segment_id != first.segment_id
    assert first.path.exists()
    assert second.path.exists()

    first_content = first.path.read_text(encoding="utf-8")
    second_content = second.path.read_text(encoding="utf-8")
    assert "第一段" in first_content
    assert "恢复后的第二段" not in first_content
    assert "恢复后的第二段" in second_content
    assert 'event_type="segment_boundary"' in first_content

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.sequence, event.event_type, event.content) for event in events] == [
        (1, "user_message", "第一段"),
        (2, "segment_boundary", "session_idle"),
        (3, "user_message", "恢复后的第二段"),
    ]


def test_stable_boundary_replay_and_target_mismatch_never_seal_new_segment(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()
    previous_run_id = uuid4()
    next_run_id = uuid4()
    previous_turn_id = f"turn-{previous_run_id.hex}"
    next_turn_id = f"turn-{next_run_id.hex}"

    _append_turn_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=previous_run_id,
        turn_id=previous_turn_id,
        role="user",
        content="previous turn",
    )
    first_receipt = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="turn_stop",
        boundary_id="boundary-previous-turn",
        idempotency_key="turn-stop:previous-turn",
        expected_runtime_task_id=previous_run_id,
        expected_turn_id=previous_turn_id,
        data_root=tmp_path,
    )
    assert first_receipt is not None

    next_user = _append_turn_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=next_run_id,
        turn_id=next_turn_id,
        role="user",
        content="next turn",
    )
    for stable_identity in (
        {"boundary_id": "boundary-previous-turn"},
        {"idempotency_key": "turn-stop:previous-turn"},
        {"boundary_id": "boundary-previous-turn", "idempotency_key": "turn-stop:previous-turn"},
    ):
        replay_receipt = seal_t0_session_segment(
            agent_id=agent_id,
            session_id=session_id,
            reason="turn_stop",
            expected_runtime_task_id=previous_run_id,
            expected_turn_id=previous_turn_id,
            data_root=tmp_path,
            **stable_identity,
        )
        assert replay_receipt == first_receipt

    with pytest.raises(T0BoundaryTargetMismatch) as identity_raised:
        seal_t0_session_segment(
            agent_id=agent_id,
            session_id=session_id,
            reason="turn_stop",
            boundary_id="boundary-previous-turn",
            idempotency_key="turn-stop:different-turn",
            expected_runtime_task_id=previous_run_id,
            expected_turn_id=previous_turn_id,
            data_root=tmp_path,
        )
    assert identity_raised.value.field == "boundary_identity"

    for suffix, expected_run_id, expected_turn_id, mismatch_field in (
        ("run", previous_run_id, next_turn_id, "runtime_task_id"),
        ("turn", next_run_id, previous_turn_id, "turn_id"),
    ):
        with pytest.raises(T0BoundaryTargetMismatch) as raised:
            seal_t0_session_segment(
                agent_id=agent_id,
                session_id=session_id,
                reason="turn_stop",
                boundary_id=f"boundary-stale-{suffix}",
                idempotency_key=f"turn-stop:stale-{suffix}",
                expected_runtime_task_id=expected_run_id,
                expected_turn_id=expected_turn_id,
                data_root=tmp_path,
            )
        assert raised.value.segment_id == next_user.segment_id
        assert raised.value.field == mismatch_field

    next_assistant = _append_turn_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=next_run_id,
        turn_id=next_turn_id,
        role="assistant",
        content="next answer",
    )
    assert next_assistant.segment_id == next_user.segment_id
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [event.event_type for event in events] == [
        "user_message",
        "segment_boundary",
        "user_message",
        "assistant_message",
    ]


def test_uuid_boundary_identity_replays_across_string_and_uuid_forms(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    turn_id = f"turn-{run_id.hex}"
    boundary_id = uuid4()
    _append_turn_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        role="user",
        content="canonical UUID boundary",
    )
    first = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="turn_stop",
        boundary_id=str(boundary_id),
        idempotency_key=f"turn-stop:{boundary_id}",
        expected_runtime_task_id=run_id,
        expected_turn_id=turn_id,
        data_root=tmp_path,
    )
    replay = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="turn_stop",
        boundary_id=boundary_id,
        idempotency_key=f"turn-stop:{boundary_id}",
        expected_runtime_task_id=run_id,
        expected_turn_id=turn_id,
        data_root=tmp_path,
    )

    assert replay == first


def test_next_run_user_event_does_not_reuse_open_segment_left_by_terminal_sidecar_crash(
    tmp_path: Path,
) -> None:
    agent_id = uuid4()
    session_id = uuid4()
    previous_run_id = uuid4()
    next_run_id = uuid4()

    previous = _append_turn_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=previous_run_id,
        turn_id=f"turn-{previous_run_id.hex}",
        role="assistant",
        content="previous committed final",
    )
    # Simulate a crash after canonical terminal commit but before TURN_STOP
    # sealed the active segment. A new run must never join that segment.
    with pytest.raises(T0SegmentBoundaryPending) as raised:
        _append_turn_event(
            data_root=tmp_path,
            agent_id=agent_id,
            session_id=session_id,
            run_id=next_run_id,
            turn_id=f"turn-{next_run_id.hex}",
            role="user",
            content="next turn",
        )

    assert raised.value.active_segment_id == previous.segment_id
    assert raised.value.active_runtime_task_id == previous_run_id.hex
    assert raised.value.incoming_runtime_task_id == next_run_id.hex
    with pytest.raises(T0SegmentBoundaryPending) as turn_raised:
        _append_turn_event(
            data_root=tmp_path,
            agent_id=agent_id,
            session_id=session_id,
            run_id=previous_run_id,
            turn_id=f"turn-{next_run_id.hex}",
            role="user",
            content="wrong turn in the same run",
        )
    assert turn_raised.value.active_turn_id == f"turn-{previous_run_id.hex}"
    assert turn_raised.value.incoming_turn_id == f"turn-{next_run_id.hex}"
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.runtime_task_id, event.content) for event in events] == [
        (previous_run_id.hex, "previous committed final")
    ]


def test_tail_replay_returns_latest_window_ascending(tmp_path: Path) -> None:
    from app.memory.t0.ledger import replay_t0_session_events_tail

    agent_id = uuid4()
    session_id = uuid4()
    for i in range(1, 16):
        append_t0_session_event(
            agent_id=agent_id,
            session_id=session_id,
            event_type="user_message",
            role="user",
            content=f"m{i}",
            data_root=tmp_path,
        )
    seal_t0_session_segment(agent_id=agent_id, session_id=session_id, reason="session_idle", data_root=tmp_path)
    for i in range(17, 31):
        append_t0_session_event(
            agent_id=agent_id,
            session_id=session_id,
            event_type="user_message",
            role="user",
            content=f"m{i}",
            data_root=tmp_path,
        )

    events = replay_t0_session_events_tail(agent_id=agent_id, session_id=session_id, limit=10, data_root=tmp_path)

    assert [event.sequence for event in events] == list(range(21, 31))


def test_tail_replay_returns_everything_when_limit_exceeds_total(tmp_path: Path) -> None:
    from app.memory.t0.ledger import replay_t0_session_events, replay_t0_session_events_tail

    agent_id = uuid4()
    session_id = uuid4()
    for i in range(1, 6):
        append_t0_session_event(
            agent_id=agent_id,
            session_id=session_id,
            event_type="user_message",
            role="user",
            content=f"m{i}",
            data_root=tmp_path,
        )

    tail = replay_t0_session_events_tail(agent_id=agent_id, session_id=session_id, limit=100, data_root=tmp_path)
    full = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)

    assert [event.sequence for event in tail] == [event.sequence for event in full]


def test_tail_replay_skips_older_segments_when_tail_is_enough(monkeypatch, tmp_path: Path) -> None:
    import app.memory.t0.ledger as ledger_module
    from app.memory.t0.ledger import replay_t0_session_events_tail

    agent_id = uuid4()
    session_id = uuid4()
    for segment in range(3):
        for i in range(10):
            append_t0_session_event(
                agent_id=agent_id,
                session_id=session_id,
                event_type="user_message",
                role="user",
                content=f"seg{segment}-m{i}",
                data_root=tmp_path,
            )
        if segment < 2:
            seal_t0_session_segment(agent_id=agent_id, session_id=session_id, reason="session_idle", data_root=tmp_path)

    parse_calls: list[str] = []
    real_parse = ledger_module._parse_events_from_jsonl

    def counting_parse(*, path, segment_id, source_path):
        parse_calls.append(segment_id)
        return real_parse(path=path, segment_id=segment_id, source_path=source_path)

    monkeypatch.setattr(ledger_module, "_parse_events_from_jsonl", counting_parse)

    events = replay_t0_session_events_tail(agent_id=agent_id, session_id=session_id, limit=5, data_root=tmp_path)

    assert len(events) == 5
    assert len(parse_calls) == 1, f"expected only the newest segment to be parsed, parsed: {parse_calls}"


def test_secret_shaped_fixture_is_preserved_in_raw_t0_source(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()
    secret = "sk-" + "A" * 24

    result = append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type="user_message",
        role="user",
        content=f"请轮换 api_key={secret}",
        data_root=tmp_path,
    )

    content = result.path.read_text(encoding="utf-8")
    assert secret in content
    assert "&lt;Credential_" not in content
    event = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)[0]
    assert secret in event.content
    assert "<Credential_" not in event.content
    assert event.sensitivity == "PL1_public"


def test_legacy_t0_file_import_is_idempotent_and_quarantined_under_session_ledger(tmp_path: Path) -> None:
    agent_id = uuid4()
    session_id = uuid4()
    legacy_path = tmp_path / str(agent_id) / "logs" / "2026-06-18" / "behavior" / "chat-1200-abcd.md"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        "---\ntype: chat\nsession_id: old-session\n---\n\n**User**: 旧日志\n**Agent**: 旧回复\n",
        encoding="utf-8",
    )

    first = import_legacy_t0_file(
        agent_id=agent_id,
        session_id=session_id,
        legacy_path=legacy_path,
        data_root=tmp_path,
    )
    second = import_legacy_t0_file(
        agent_id=agent_id,
        session_id=session_id,
        legacy_path=legacy_path,
        data_root=tmp_path,
    )

    assert first.segment_id == second.segment_id
    assert first.imported is True
    assert second.imported is False
    assert (
        first.path
        == tmp_path
        / str(agent_id)
        / "memory"
        / "t0"
        / "sessions"
        / str(session_id)
        / "segments"
        / first.segment_id
        / "source.md"
    )
    assert first.path.exists()
    assert legacy_path.exists(), "legacy import must never delete or rewrite the source file"

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert len([event for event in events if event.event_type == "legacy_import"]) == 1
    assert events[0].metadata["legacy_path"].endswith("logs/2026-06-18/behavior/chat-1200-abcd.md")


def _append_run_event(
    *,
    data_root: Path,
    agent_id: UUID,
    session_id: UUID,
    run_id: UUID,
    role: str,
    content: str,
):
    # Production bridge shape: events carry the exact RuntimeTask identity but
    # no turn metadata anywhere (top-level or metadata).
    return append_t0_session_event(
        agent_id=agent_id,
        session_id=session_id,
        event_type=f"{role}_message",
        role=role,
        content=content,
        runtime_task_id=run_id,
        source="web",
        data_root=data_root,
    )


def test_canonical_terminal_adopts_idle_sealed_segment_idempotently(tmp_path: Path) -> None:
    """An idle seal that wins the terminal race must not strand the canonical terminal.

    SESSION_IDLE may seal the last open segment with an ordinary boundary event
    before the canonical terminal outbox item settles.  This reproduces the
    verified production shape: the segment and its boundary events carry the
    exact RuntimeTask identity but no turn identity at all.  Only an already
    validated durable caller/outbox identity may request this stateless
    receipt: the canonical redrive must still recognize and reuse that exact
    sealed segment (same real boundary event ID and sequence, no second
    terminal boundary, no fabricated turn persisted or bound), expose the
    caller-proven canonical UUID boundary identity, stay idempotent on
    further redrives without any index mutation, and refuse wrong-run /
    partial-identity / already-stable-sealed adoption.
    """
    agent_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    # The caller still proves a non-empty DB-canonical turn; the segment
    # simply has no stored turn to check it against.
    turn_id = f"turn-{run_id.hex}"

    _append_run_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        role="user",
        content="canonical terminal turn",
    )
    _append_run_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        role="assistant",
        content="final answer",
    )

    idle_seal = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="session_idle",
        data_root=tmp_path,
    )
    assert idle_seal is not None
    # An idle seal mints a non-canonical evt_<hex> boundary identity; the
    # canonical receipt must never leak it as the boundary identity.
    assert idle_seal.boundary_id is not None
    assert idle_seal.boundary_id.startswith("evt_")

    events_before = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    boundary_events_before = [event for event in events_before if event.event_type == "segment_boundary"]
    assert len(boundary_events_before) == 1
    index_path = tmp_path / str(agent_id) / "memory" / "t0" / "sessions" / str(session_id) / "index.json"
    index_before = index_path.read_text(encoding="utf-8")

    boundary_id = str(uuid4())
    canonical = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="canonical_terminal_boundary",
        boundary_id=boundary_id,
        idempotency_key="terminal-boundary:1",
        expected_runtime_task_id=run_id,
        expected_turn_id=turn_id,
        data_root=tmp_path,
    )
    assert canonical is not None
    assert canonical.segment_id == idle_seal.segment_id
    assert canonical.event_id == idle_seal.event_id
    assert canonical.sequence == idle_seal.sequence
    assert canonical.boundary_id == boundary_id

    events_after = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert events_after == events_before, "adoption must not append or rewrite any T0 event"
    assert index_path.read_text(encoding="utf-8") == index_before, "adoption must not write the session index"

    # The supported terminal outbox redrive always carries both deterministic
    # identities; repeating it re-derives the identical canonical receipt.
    replay = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="canonical_terminal_boundary",
        boundary_id=boundary_id,
        idempotency_key="terminal-boundary:1",
        expected_runtime_task_id=run_id,
        expected_turn_id=turn_id,
        data_root=tmp_path,
    )
    assert replay == canonical
    assert index_path.read_text(encoding="utf-8") == index_before

    # Partial identity (either half alone) fails closed instead of adopting.
    for partial in ({"boundary_id": boundary_id}, {"idempotency_key": "terminal-boundary:1"}):
        assert (
            seal_t0_session_segment(
                agent_id=agent_id,
                session_id=session_id,
                reason="canonical_terminal_boundary",
                expected_runtime_task_id=run_id,
                expected_turn_id=turn_id,
                data_root=tmp_path,
                **partial,
            )
            is None
        )

    # Wrong run never adopts the sealed segment.
    assert (
        seal_t0_session_segment(
            agent_id=agent_id,
            session_id=session_id,
            reason="canonical_terminal_boundary",
            boundary_id=str(uuid4()),
            idempotency_key="terminal-boundary:3",
            expected_runtime_task_id=uuid4(),
            expected_turn_id=turn_id,
            data_root=tmp_path,
        )
        is None
    )

    # A newer open segment after the idle seal means the active segment no
    # longer proves this terminal; sealing it for another run fails closed.
    next_run = uuid4()
    next_turn = f"turn-{next_run.hex}"
    _append_turn_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=next_run,
        turn_id=next_turn,
        role="user",
        content="next turn starts",
    )
    with pytest.raises(T0BoundaryTargetMismatch) as raised:
        seal_t0_session_segment(
            agent_id=agent_id,
            session_id=session_id,
            reason="canonical_terminal_boundary",
            boundary_id=str(uuid4()),
            idempotency_key="terminal-boundary:5",
            expected_runtime_task_id=run_id,
            expected_turn_id=turn_id,
            data_root=tmp_path,
        )
    assert raised.value.field == "runtime_task_id"

    final_events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert len([event for event in final_events if event.event_type == "segment_boundary"]) == 1


def test_adoption_fails_closed_when_bound_turn_mismatches(tmp_path: Path) -> None:
    """A segment that does store a turn still rejects a wrong-turn adoption."""

    agent_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    turn_id = f"turn-{run_id.hex}"
    _append_turn_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        role="user",
        content="bound turn",
    )
    idle_seal = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="session_idle",
        data_root=tmp_path,
    )
    assert idle_seal is not None

    with pytest.raises(T0BoundaryTargetMismatch) as raised:
        seal_t0_session_segment(
            agent_id=agent_id,
            session_id=session_id,
            reason="canonical_terminal_boundary",
            boundary_id=str(uuid4()),
            idempotency_key="terminal-boundary:bound-turn",
            expected_runtime_task_id=run_id,
            expected_turn_id=f"turn-{uuid4().hex}",
            data_root=tmp_path,
        )
    assert raised.value.field == "turn_id"


def test_adoption_fails_closed_when_stale_active_pointer_names_latest_segment(tmp_path: Path) -> None:
    """A stale index pointer that still names the latest sealed segment blocks adoption."""

    agent_id = uuid4()
    session_id = uuid4()
    run_id = uuid4()
    turn_id = f"turn-{run_id.hex}"
    _append_turn_event(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        role="user",
        content="turn",
    )
    idle_seal = seal_t0_session_segment(
        agent_id=agent_id,
        session_id=session_id,
        reason="session_idle",
        data_root=tmp_path,
    )
    assert idle_seal is not None

    index_path = tmp_path / str(agent_id) / "memory" / "t0" / "sessions" / str(session_id) / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["active_segment_id"] = idle_seal.segment_id
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    stale_index = index_path.read_text(encoding="utf-8")

    assert (
        seal_t0_session_segment(
            agent_id=agent_id,
            session_id=session_id,
            reason="canonical_terminal_boundary",
            boundary_id=str(uuid4()),
            idempotency_key="terminal-boundary:1",
            expected_runtime_task_id=run_id,
            expected_turn_id=turn_id,
            data_root=tmp_path,
        )
        is None
    )
    assert index_path.read_text(encoding="utf-8") == stale_index, "fail-closed adoption must not rewrite the index"
