"""Append-only T0 session ledger.

This is Hive's raw conversation substrate. It follows the same ground rule as
Claude Code transcripts and Codex rollouts: accepted session events are appended
to JSONL mechanical truth first, then a deterministic Markdown/XML projection
and higher-layer indexes may be built from it. T0 is not a summary store and
does not rewrite historical events.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from app.config import get_settings
from app.memory.form_lint import lint_memory_form
from app.services.knowledge_provenance import enrich_knowledge_event_metadata
from app.services.privacy_layer import PrivacyLayer, PrivacyStore, canonicalize_sensitivity, max_sensitivity

try:  # pragma: no cover - Windows fallback; production/dev targets are Unix.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


SCHEMA_VERSION = "t0.session-ledger.v1"
EVENT_RECORD_SCHEMA_VERSION = "t0.event-record.v2"
MARKDOWN_PROJECTION_SCHEMA_VERSION = "t0.markdown-projection.v1"
SOURCE_FILENAME = "source.md"
EVENTS_FILENAME = "events.jsonl"
INDEX_FILENAME = "index.json"
_EVENT_BLOCK_RE = re.compile(r"<t0_event\b.*?</t0_event>", re.DOTALL)


@dataclass(frozen=True, slots=True)
class T0AppendResult:
    path: Path
    jsonl_path: Path
    segment_id: str
    event_id: str
    sequence: int


class T0SegmentBoundaryPending(RuntimeError):
    """A new turn cannot append until the currently open segment is sealed."""

    code = "t0_previous_segment_boundary_pending"
    retryable = True

    def __init__(
        self,
        *,
        active_segment_id: str,
        active_runtime_task_id: str | None,
        active_turn_id: str | None,
        incoming_runtime_task_id: str | None,
        incoming_turn_id: str | None,
    ) -> None:
        self.active_segment_id = active_segment_id
        self.active_runtime_task_id = active_runtime_task_id
        self.active_turn_id = active_turn_id
        self.incoming_runtime_task_id = incoming_runtime_task_id
        self.incoming_turn_id = incoming_turn_id
        super().__init__(
            f"{self.code}: active_segment_id={active_segment_id} "
            f"active_runtime_task_id={active_runtime_task_id!r} active_turn_id={active_turn_id!r} "
            f"incoming_runtime_task_id={incoming_runtime_task_id!r} incoming_turn_id={incoming_turn_id!r}"
        )


class T0BoundaryTargetMismatch(RuntimeError):
    """A boundary command targets a different segment owner."""

    code = "t0_boundary_target_mismatch"
    retryable = False

    def __init__(self, *, segment_id: str, field: str, expected: str, actual: str | None) -> None:
        self.segment_id = segment_id
        self.field = field
        self.expected = expected
        self.actual = actual
        super().__init__(f"{self.code}: segment_id={segment_id} field={field} expected={expected!r} actual={actual!r}")


@dataclass(frozen=True, slots=True)
class T0SealResult:
    path: Path
    segment_id: str
    sequence: int
    jsonl_path: Path | None = None
    event_id: str = ""
    boundary_id: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyImportResult:
    path: Path
    segment_id: str
    sequence: int
    imported: bool
    jsonl_path: Path | None = None


@dataclass(frozen=True, slots=True)
class T0SessionEvent:
    event_id: str
    sequence: int
    event_type: str
    role: str | None
    content: str
    created_at: str
    message_id: str | None
    actor_id: str | None
    runtime_task_id: str | None
    turn_id: str | None
    intent_id: str | None
    source: str
    sensitivity: str
    metadata: dict[str, Any]
    path: Path
    segment_id: str
    truth_path: Path | None = None
    byte_offset: int | None = None
    byte_length: int | None = None
    event_hash: str | None = None
    prev_event_hash: str | None = None
    record_schema_version: str = MARKDOWN_PROJECTION_SCHEMA_VERSION


def append_t0_session_event(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    event_type: str,
    role: str | None = None,
    content: Any = "",
    message_id: uuid.UUID | str | None = None,
    actor_id: uuid.UUID | str | None = None,
    tenant_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    source: str = "runtime",
    metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    data_root: Path | str | None = None,
) -> T0AppendResult:
    """Append one raw session event to the active T0 segment."""

    now = _utc_now(created_at)
    session_dir = _session_dir(data_root, agent_id, session_id)
    index = _load_or_create_index(session_dir, agent_id=agent_id, session_id=session_id, now=now)
    event_metadata = _clean_metadata(metadata)
    event_metadata = enrich_knowledge_event_metadata(
        event_type=event_type,
        content=content,
        metadata=event_metadata,
    )
    incoming_runtime_task_id = _canonical_runtime_task_id(
        runtime_task_id or _metadata_text(event_metadata, "runtime_task_id") or None
    )
    incoming_turn_id = _metadata_text(event_metadata, "turn_id") or None
    segment = _ensure_open_segment(
        index,
        now=now,
        runtime_task_id=incoming_runtime_task_id,
        turn_id=incoming_turn_id,
    )
    sequence = int(index.get("next_sequence") or 1)
    path = session_dir / segment["path"]
    jsonl_path = session_dir / _segment_events_path(segment)
    event_id = _new_event_id()
    sanitized_content, detected_sensitivity, form_warnings = _sanitize_t0_content(content)
    declared_sensitivity = event_metadata.get("content_sensitivity")
    try:
        sensitivity = (
            max_sensitivity(detected_sensitivity, declared_sensitivity).value
            if declared_sensitivity is not None
            else canonicalize_sensitivity(detected_sensitivity).value
        )
    except ValueError:
        # Invalid typed provenance is a machine-contract failure. Preserve the
        # evidence and fail closed for every downstream durable consumer.
        sensitivity = "PL4_credential"
        event_metadata["semantic_memory_eligible"] = False
        event_metadata["content_sensitivity"] = sensitivity
        event_metadata["sensitivity_contract_error"] = "invalid_declared_sensitivity"
    if form_warnings:
        event_metadata["form_warnings"] = form_warnings
    event_record = _build_event_record(
        agent_id=agent_id,
        session_id=session_id,
        segment_id=str(segment["segment_id"]),
        source_path=Path(str(segment["path"])),
        jsonl_path=_segment_events_path(segment),
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        role=role,
        content=sanitized_content,
        created_at=now,
        message_id=message_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        runtime_task_id=runtime_task_id,
        source=source,
        sensitivity=sensitivity,
        metadata=event_metadata,
        prev_event_hash=str(index.get("last_event_hash") or ""),
    )
    _append_event_record(jsonl_path, event_record)
    _append_event_block(
        path,
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        role=role,
        content=sanitized_content,
        created_at=now,
        message_id=message_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        runtime_task_id=runtime_task_id,
        source=source,
        sensitivity=sensitivity,
        metadata=event_metadata,
    )
    segment["events_path"] = _segment_events_path(segment).as_posix()
    segment["last_event_hash"] = event_record["event_hash"]
    _record_turn_start_metadata(
        segment,
        event_id=event_id,
        event_type=event_type,
        role=role,
        metadata=event_metadata,
    )
    index["next_sequence"] = sequence + 1
    index["updated_at"] = _iso(now)
    index["truth_surface"] = "events.jsonl"
    index["projection_surface"] = "source.md"
    index["last_event_hash"] = event_record["event_hash"]
    _write_index(session_dir, index)
    return T0AppendResult(
        path=path,
        jsonl_path=jsonl_path,
        segment_id=str(segment["segment_id"]),
        event_id=event_id,
        sequence=sequence,
    )


def seal_t0_session_segment(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    reason: str,
    metadata: dict[str, Any] | None = None,
    boundary_id: uuid.UUID | str | None = None,
    idempotency_key: str | None = None,
    expected_runtime_task_id: uuid.UUID | str | None = None,
    expected_turn_id: str | None = None,
    data_root: Path | str | None = None,
    created_at: datetime | None = None,
) -> T0SealResult | None:
    """Seal the active segment by appending a boundary event.

    Sealing creates a resume boundary; it does not end or summarize the DB
    ChatSession. A stable boundary identity replays its original receipt even
    when a newer segment is active. Expected target mismatches fail before any
    append, so recovery can never seal the wrong turn.
    """

    now = _utc_now(created_at)
    session_dir = _session_dir(data_root, agent_id, session_id)
    index = _load_or_create_index(session_dir, agent_id=agent_id, session_id=session_id, now=now)
    requested_boundary_id, idempotency_key_hash = _boundary_identity(
        boundary_id=boundary_id,
        idempotency_key=idempotency_key,
    )
    event_metadata_input = _clean_metadata(metadata)
    expected_runtime_task_id_value = _canonical_runtime_task_id(expected_runtime_task_id)
    expected_turn_id_value = _optional_text(expected_turn_id)
    replay_segment = _find_boundary_segment(
        index,
        boundary_id=requested_boundary_id,
        idempotency_key_hash=idempotency_key_hash,
    )
    if replay_segment is not None:
        _validate_boundary_target(
            replay_segment,
            expected_runtime_task_id=expected_runtime_task_id_value,
            expected_turn_id=expected_turn_id_value,
            bind_missing=False,
        )
        return _seal_result_from_segment(session_dir, replay_segment)

    active_segment_id = index.get("active_segment_id")
    if not active_segment_id:
        adopted = _adopt_pre_sealed_segment(
            index,
            boundary_id=requested_boundary_id,
            idempotency_key_hash=idempotency_key_hash,
            expected_runtime_task_id=expected_runtime_task_id_value,
            expected_turn_id=expected_turn_id_value,
        )
        if adopted is None:
            return None
        # Adoption is a pure derivation from the immutable sealed segment: no
        # index mutation and no index write, so a concurrent append/seal can
        # never lose its own index update to an adoption.
        return _seal_result_from_segment(session_dir, adopted, boundary_id=requested_boundary_id)
    segment = _segment_by_id(index, str(active_segment_id))
    if segment is None or segment.get("state") != "open":
        # The pointer is deliberately left untouched here: a stale pointer that
        # still names the latest segment is a fail-closed adoption condition,
        # and any other stale pointer self-heals on the next append.
        adopted = _adopt_pre_sealed_segment(
            index,
            boundary_id=requested_boundary_id,
            idempotency_key_hash=idempotency_key_hash,
            expected_runtime_task_id=expected_runtime_task_id_value,
            expected_turn_id=expected_turn_id_value,
        )
        if adopted is not None:
            return _seal_result_from_segment(session_dir, adopted, boundary_id=requested_boundary_id)
        return None

    _validate_boundary_target(
        segment,
        expected_runtime_task_id=expected_runtime_task_id_value,
        expected_turn_id=expected_turn_id_value,
        bind_missing=True,
    )

    sequence = int(index.get("next_sequence") or 1)
    path = session_dir / segment["path"]
    jsonl_path = session_dir / _segment_events_path(segment)
    event_id = _new_event_id()
    effective_boundary_id = (
        requested_boundary_id or (f"boundary_{idempotency_key_hash[:32]}" if idempotency_key_hash else None) or event_id
    )
    event_metadata = _boundary_metadata(reason=reason, event_id=event_id, metadata=event_metadata_input)
    event_metadata["boundary_id"] = effective_boundary_id
    if idempotency_key_hash:
        event_metadata["boundary_idempotency_key_sha256"] = idempotency_key_hash
    segment_runtime_task_id = _canonical_runtime_task_id(segment.get("runtime_task_id"))
    segment_turn_id = _optional_text(segment.get("turn_id"))
    if segment_runtime_task_id:
        event_metadata["runtime_task_id"] = segment_runtime_task_id
    if segment_turn_id:
        event_metadata["turn_id"] = segment_turn_id
    event_record = _build_event_record(
        agent_id=agent_id,
        session_id=session_id,
        segment_id=str(segment["segment_id"]),
        source_path=Path(str(segment["path"])),
        jsonl_path=_segment_events_path(segment),
        event_id=event_id,
        sequence=sequence,
        event_type="segment_boundary",
        role="system",
        content=reason,
        created_at=now,
        message_id=None,
        actor_id=None,
        tenant_id=None,
        runtime_task_id=segment_runtime_task_id,
        source="t0_ledger",
        sensitivity="PL1_public",
        metadata=event_metadata,
        prev_event_hash=str(index.get("last_event_hash") or ""),
    )
    _append_event_record(jsonl_path, event_record)
    _append_event_block(
        path,
        event_id=event_id,
        sequence=sequence,
        event_type="segment_boundary",
        role="system",
        content=reason,
        created_at=now,
        message_id=None,
        actor_id=None,
        tenant_id=None,
        runtime_task_id=segment_runtime_task_id,
        source="t0_ledger",
        sensitivity="PL1_public",
        metadata=event_metadata,
    )
    segment["events_path"] = _segment_events_path(segment).as_posix()
    segment["last_event_hash"] = event_record["event_hash"]
    segment["state"] = "sealed"
    segment["sealed_at"] = _iso(now)
    segment["seal_reason"] = reason
    segment["boundary_id"] = effective_boundary_id
    segment["boundary_idempotency_key_sha256"] = idempotency_key_hash
    segment["boundary_sequence"] = sequence
    _record_turn_boundary_metadata(segment, event_id=event_id, metadata=event_metadata)
    index["active_segment_id"] = None
    index["next_sequence"] = sequence + 1
    index["updated_at"] = _iso(now)
    index["truth_surface"] = "events.jsonl"
    index["projection_surface"] = "source.md"
    index["last_event_hash"] = event_record["event_hash"]
    _write_index(session_dir, index)
    return T0SealResult(
        path=path,
        segment_id=str(segment["segment_id"]),
        sequence=sequence,
        jsonl_path=jsonl_path,
        event_id=event_id,
        boundary_id=effective_boundary_id,
    )


def import_legacy_t0_file(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    legacy_path: Path | str,
    data_root: Path | str | None = None,
    created_at: datetime | None = None,
) -> LegacyImportResult:
    """Import one legacy logs/YYYY-MM-DD/* file into a sealed session segment.

    The source file is never modified or deleted. Idempotency is keyed by the
    source path plus content digest, so repeated repair runs cannot duplicate
    history in the new ledger.
    """

    now = _utc_now(created_at)
    source_path = Path(legacy_path)
    raw = source_path.read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    legacy_key = hashlib.sha256(f"{source_path.resolve()}\0{digest}".encode("utf-8")).hexdigest()
    session_dir = _session_dir(data_root, agent_id, session_id)
    index = _load_or_create_index(session_dir, agent_id=agent_id, session_id=session_id, now=now)
    legacy_imports = dict(index.get("legacy_imports") or {})
    if legacy_key in legacy_imports:
        record = legacy_imports[legacy_key]
        return LegacyImportResult(
            path=session_dir / str(record["path"]),
            segment_id=str(record["segment_id"]),
            sequence=int(record["sequence"]),
            imported=False,
            jsonl_path=session_dir / str(record.get("events_path") or _segment_events_path(record)),
        )

    segment_id = f"legacy-{digest[:12]}"
    segment_path = Path("segments") / segment_id / SOURCE_FILENAME
    segment = _segment_by_id(index, segment_id)
    if segment is None:
        segment = {
            "segment_id": segment_id,
            "path": segment_path.as_posix(),
            "events_path": (Path("segments") / segment_id / EVENTS_FILENAME).as_posix(),
            "state": "sealed",
            "created_at": _iso(now),
            "sealed_at": _iso(now),
            "seal_reason": "legacy_import",
            "origin": "legacy_t0_log",
        }
        index.setdefault("segments", []).append(segment)
        _ensure_segment_file(
            session_dir / segment_path, agent_id=agent_id, session_id=session_id, segment_id=segment_id, now=now
        )

    sequence = int(index.get("next_sequence") or 1)
    path = session_dir / segment_path
    jsonl_path = session_dir / _segment_events_path(segment)
    event_id = _new_event_id()
    metadata = {
        "legacy_path": source_path.as_posix(),
        "legacy_sha256": digest,
    }
    event_record = _build_event_record(
        agent_id=agent_id,
        session_id=session_id,
        segment_id=segment_id,
        source_path=segment_path,
        jsonl_path=_segment_events_path(segment),
        event_id=event_id,
        sequence=sequence,
        event_type="legacy_import",
        role="system",
        content=raw,
        created_at=now,
        message_id=None,
        actor_id=None,
        tenant_id=None,
        runtime_task_id=None,
        source="legacy_t0_logger",
        sensitivity="PL1_public",
        metadata=metadata,
        prev_event_hash=str(index.get("last_event_hash") or ""),
    )
    _append_event_record(jsonl_path, event_record)
    _append_event_block(
        path,
        event_id=event_id,
        sequence=sequence,
        event_type="legacy_import",
        role="system",
        content=raw,
        created_at=now,
        message_id=None,
        actor_id=None,
        tenant_id=None,
        runtime_task_id=None,
        source="legacy_t0_logger",
        sensitivity="PL1_public",
        metadata=metadata,
    )
    segment["events_path"] = _segment_events_path(segment).as_posix()
    segment["last_event_hash"] = event_record["event_hash"]
    legacy_imports[legacy_key] = {
        "segment_id": segment_id,
        "path": segment_path.as_posix(),
        "events_path": _segment_events_path(segment).as_posix(),
        "sequence": sequence,
        "legacy_path": source_path.as_posix(),
        "sha256": digest,
        "imported_at": _iso(now),
    }
    index["legacy_imports"] = legacy_imports
    index["next_sequence"] = sequence + 1
    index["updated_at"] = _iso(now)
    index["truth_surface"] = "events.jsonl"
    index["projection_surface"] = "source.md"
    index["last_event_hash"] = event_record["event_hash"]
    _write_index(session_dir, index)
    return LegacyImportResult(path=path, segment_id=segment_id, sequence=sequence, imported=True, jsonl_path=jsonl_path)


def _replay_segment_events(*, session_dir: Path, segment: dict[str, Any]) -> list[T0SessionEvent]:
    segment_id = str(segment.get("segment_id") or "")
    rel_path = segment.get("path") or (Path("segments") / segment_id / SOURCE_FILENAME).as_posix()
    source_path = session_dir / str(rel_path)
    jsonl_path = session_dir / _segment_events_path(segment)
    if jsonl_path.exists():
        return _parse_events_from_jsonl(path=jsonl_path, segment_id=segment_id, source_path=source_path)
    if source_path.exists():
        return _parse_events_from_source(path=source_path, segment_id=segment_id)
    return []


def replay_t0_session_events(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    data_root: Path | str | None = None,
) -> list[T0SessionEvent]:
    """Read a T0 session ledger back into ordered raw events."""

    session_dir = _session_dir(data_root, agent_id, session_id)
    index = _read_index(session_dir)
    segment_records = list(index.get("segments") or []) if index else _discover_segments(session_dir)
    events: list[T0SessionEvent] = []
    for segment in segment_records:
        events.extend(_replay_segment_events(session_dir=session_dir, segment=segment))
    return sorted(events, key=lambda event: event.sequence)


def replay_t0_session_events_tail(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    limit: int,
    data_root: Path | str | None = None,
) -> list[T0SessionEvent]:
    """Read only the newest ``limit`` events, ascending by sequence.

    Walks index segments newest-first (index order is append order, and
    sequences are globally monotonic) and stops as soon as the tail is
    covered, so long sessions do not pay a full-ledger parse. Sessions
    without an index fall back to a full replay — discovered segment
    directory names are not guaranteed to sort chronologically.
    """
    if limit <= 0:
        return []
    session_dir = _session_dir(data_root, agent_id, session_id)
    index = _read_index(session_dir)
    if not index:
        events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=data_root)
        return events[-limit:]
    collected: list[T0SessionEvent] = []
    for segment in reversed(list(index.get("segments") or [])):
        collected.extend(_replay_segment_events(session_dir=session_dir, segment=segment))
        if len(collected) >= limit:
            break
    collected.sort(key=lambda event: event.sequence)
    return collected[-limit:]


def _session_dir(data_root: Path | str | None, agent_id: uuid.UUID | str, session_id: uuid.UUID | str) -> Path:
    return _data_root(data_root) / str(agent_id) / "memory" / "t0" / "sessions" / str(session_id)


def _data_root(data_root: Path | str | None) -> Path:
    return Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)


def _load_or_create_index(
    session_dir: Path,
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    now: datetime,
) -> dict[str, Any]:
    existing = _read_index(session_dir)
    if existing:
        return existing
    session_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "schema_version": SCHEMA_VERSION,
        "event_record_schema_version": EVENT_RECORD_SCHEMA_VERSION,
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "created_at": _iso(now),
        "updated_at": _iso(now),
        "truth_surface": "events.jsonl",
        "projection_surface": "source.md",
        "active_segment_id": None,
        "next_sequence": 1,
        "last_event_hash": None,
        "segments": [],
        "legacy_imports": {},
    }
    _write_index(session_dir, index)
    return index


def _read_index(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / INDEX_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_index(session_dir: Path, index: dict[str, Any]) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / INDEX_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _boundary_identity(
    *,
    boundary_id: uuid.UUID | str | None,
    idempotency_key: str | None,
) -> tuple[str | None, str | None]:
    boundary_id_value = _canonical_boundary_id(boundary_id)
    idempotency_key_value = _optional_text(idempotency_key)
    idempotency_key_hash = (
        hashlib.sha256(idempotency_key_value.encode("utf-8")).hexdigest() if idempotency_key_value else None
    )
    return boundary_id_value, idempotency_key_hash


def _adopt_pre_sealed_segment(
    index: dict[str, Any],
    *,
    boundary_id: str | None,
    idempotency_key_hash: str | None,
    expected_runtime_task_id: str | None,
    expected_turn_id: str | None,
) -> dict[str, Any] | None:
    """Recognize an already-sealed segment as the caller's terminal seal.

    SESSION_IDLE/SESSION_CLOSE may seal the last open segment with an ordinary
    boundary event while a canonical terminal outbox item is still pending. A
    later canonical redrive must not append a second terminal boundary or mint
    a new receipt: when the caller proves the exact runtime task and turn that
    own the latest sealed segment, the existing seal becomes the terminal seal
    and the receipt carries the caller-proven canonical boundary identity
    while preserving the real boundary event ID and sequence.

    Adoption is a pure derivation. It mutates no segment state and writes no
    index: only an already validated durable caller/outbox identity may
    request this stateless receipt, and a repeated redrive re-derives the
    identical receipt from the same immutable seal. Anything unproven returns
    None so the caller fails closed.
    """
    if boundary_id is None or idempotency_key_hash is None:
        return None
    if expected_runtime_task_id is None or expected_turn_id is None:
        return None
    try:
        uuid.UUID(boundary_id)
    except (ValueError, AttributeError, TypeError):
        # The receipt must expose a canonical UUID boundary identity.
        return None
    segments = list(index.get("segments") or [])
    if not segments:
        return None
    segment = segments[-1]
    if str(segment.get("segment_id")) == str(index.get("active_segment_id") or ""):
        return None
    if segment.get("state") != "sealed":
        return None
    # A segment sealed under a stable idempotency key belongs to that lane; a
    # different canonical identity must not re-own it.
    if _optional_text(segment.get("boundary_idempotency_key_sha256")) is not None:
        return None
    if _optional_text(segment.get("boundary_event_id")) is None or segment.get("boundary_sequence") is None:
        # Ambiguous legacy seal shape: there is no provable boundary event.
        return None
    if _canonical_runtime_task_id(segment.get("runtime_task_id")) != expected_runtime_task_id:
        return None
    segment_turn_id = _optional_text(segment.get("turn_id"))
    if segment_turn_id is None:
        # Legacy/current bridge shape: the segment and its boundary events may
        # predate turn indexing entirely. Exact runtime-task equality has
        # already proven ownership (one RuntimeTask is one turn), so recovery
        # is not rejected for the missing turn truth. Adoption is a pure
        # derivation, so no turn is persisted or bound to the segment.
        return segment
    if segment_turn_id != expected_turn_id:
        raise T0BoundaryTargetMismatch(
            segment_id=str(segment.get("segment_id") or ""),
            field="turn_id",
            expected=expected_turn_id,
            actual=segment_turn_id,
        )
    return segment


def _find_boundary_segment(
    index: dict[str, Any],
    *,
    boundary_id: str | None,
    idempotency_key_hash: str | None,
) -> dict[str, Any] | None:
    if boundary_id is None and idempotency_key_hash is None:
        return None
    segments = list(index.get("segments") or [])
    boundary_matches = [
        segment
        for segment in segments
        if boundary_id is not None and _canonical_boundary_id(segment.get("boundary_id")) == boundary_id
    ]
    key_matches = [
        segment
        for segment in segments
        if idempotency_key_hash is not None
        and _optional_text(segment.get("boundary_idempotency_key_sha256")) == idempotency_key_hash
    ]
    if len(boundary_matches) > 1 or len(key_matches) > 1:
        matches = boundary_matches if len(boundary_matches) > 1 else key_matches
        raise T0BoundaryTargetMismatch(
            segment_id=str(matches[0].get("segment_id") or ""),
            field="boundary_identity",
            expected=boundary_id or str(idempotency_key_hash),
            actual=",".join(str(segment.get("segment_id") or "") for segment in matches),
        )
    if boundary_id is not None and idempotency_key_hash is not None:
        if not boundary_matches and not key_matches:
            return None
        if boundary_matches and key_matches and boundary_matches[0] is key_matches[0]:
            return boundary_matches[0]
        matches = boundary_matches or key_matches
        raise T0BoundaryTargetMismatch(
            segment_id=str(matches[0].get("segment_id") or ""),
            field="boundary_identity",
            expected=f"{boundary_id}:{idempotency_key_hash}",
            actual=",".join(str(segment.get("segment_id") or "") for segment in boundary_matches + key_matches),
        )
    matches = boundary_matches or key_matches
    return matches[0] if matches else None


def _canonical_boundary_id(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text


def _validate_boundary_target(
    segment: dict[str, Any],
    *,
    expected_runtime_task_id: str | None,
    expected_turn_id: str | None,
    bind_missing: bool,
) -> None:
    targets = (
        ("runtime_task_id", expected_runtime_task_id, _canonical_runtime_task_id(segment.get("runtime_task_id"))),
        ("turn_id", expected_turn_id, _optional_text(segment.get("turn_id"))),
    )
    for field, expected, actual in targets:
        if expected is None:
            continue
        if actual is not None and actual != expected or actual is None and not bind_missing:
            raise T0BoundaryTargetMismatch(
                segment_id=str(segment.get("segment_id") or ""),
                field=field,
                expected=expected,
                actual=actual,
            )
        if actual is None:
            segment[field] = expected


def _seal_result_from_segment(
    session_dir: Path,
    segment: dict[str, Any],
    *,
    boundary_id: str | None = None,
) -> T0SealResult:
    return T0SealResult(
        path=session_dir / str(segment["path"]),
        segment_id=str(segment["segment_id"]),
        sequence=int(segment["boundary_sequence"]),
        jsonl_path=session_dir / _segment_events_path(segment),
        event_id=str(segment["boundary_event_id"]),
        boundary_id=boundary_id or _optional_text(segment.get("boundary_id")),
    )


def _ensure_open_segment(
    index: dict[str, Any],
    *,
    now: datetime,
    runtime_task_id: str | None,
    turn_id: str | None,
) -> dict[str, Any]:
    active_segment_id = index.get("active_segment_id")
    if active_segment_id:
        segment = _segment_by_id(index, str(active_segment_id))
        if segment is not None and segment.get("state") == "open":
            active_runtime_task_id = _canonical_runtime_task_id(segment.get("runtime_task_id"))
            active_turn_id = _optional_text(segment.get("turn_id"))
            runtime_mismatch = bool(
                active_runtime_task_id and runtime_task_id and active_runtime_task_id != runtime_task_id
            )
            turn_mismatch = bool(active_turn_id and turn_id and active_turn_id != turn_id)
            if runtime_mismatch or turn_mismatch:
                raise T0SegmentBoundaryPending(
                    active_segment_id=str(segment["segment_id"]),
                    active_runtime_task_id=active_runtime_task_id,
                    active_turn_id=active_turn_id,
                    incoming_runtime_task_id=runtime_task_id,
                    incoming_turn_id=turn_id,
                )
            if runtime_task_id and not active_runtime_task_id:
                segment["runtime_task_id"] = runtime_task_id
            if turn_id and not active_turn_id:
                segment["turn_id"] = turn_id
            return segment

    segment_id = f"seg-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    segment = {
        "segment_id": segment_id,
        "path": (Path("segments") / segment_id / SOURCE_FILENAME).as_posix(),
        "events_path": (Path("segments") / segment_id / EVENTS_FILENAME).as_posix(),
        "state": "open",
        "created_at": _iso(now),
        "sealed_at": None,
        "origin": "session",
    }
    if runtime_task_id:
        segment["runtime_task_id"] = runtime_task_id
    if turn_id:
        segment["turn_id"] = turn_id
    index.setdefault("segments", []).append(segment)
    index["active_segment_id"] = segment_id
    return segment


def _segment_by_id(index: dict[str, Any], segment_id: str) -> dict[str, Any] | None:
    for segment in index.get("segments") or []:
        if str(segment.get("segment_id")) == segment_id:
            return segment
    return None


def _segment_events_path(segment: dict[str, Any]) -> Path:
    raw = segment.get("events_path")
    if raw:
        return Path(str(raw))
    segment_id = str(segment.get("segment_id") or "")
    return Path("segments") / segment_id / EVENTS_FILENAME


def _build_event_record(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    segment_id: str,
    source_path: Path,
    jsonl_path: Path,
    event_id: str,
    sequence: int,
    event_type: str,
    role: str | None,
    content: str,
    created_at: datetime,
    message_id: uuid.UUID | str | None,
    actor_id: uuid.UUID | str | None,
    tenant_id: uuid.UUID | str | None,
    runtime_task_id: uuid.UUID | str | None,
    source: str,
    sensitivity: str,
    metadata: dict[str, Any],
    prev_event_hash: str,
) -> dict[str, Any]:
    runtime_task_id_value = _id_value(runtime_task_id) or _metadata_text(metadata, "runtime_task_id") or None
    record: dict[str, Any] = {
        "schema_version": EVENT_RECORD_SCHEMA_VERSION,
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "segment_id": segment_id,
        "event_id": event_id,
        "sequence": sequence,
        "event_type": event_type,
        "role": role,
        "content": content,
        "created_at": _iso(created_at),
        "message_id": _id_value(message_id) or None,
        "actor_id": _id_value(actor_id) or None,
        "tenant_id": _id_value(tenant_id) or None,
        "runtime_task_id": runtime_task_id_value,
        "turn_id": _metadata_text(metadata, "turn_id") or None,
        "intent_id": _metadata_text(metadata, "intent_id") or None,
        "source": source,
        "sensitivity": sensitivity,
        "metadata": metadata,
        "prev_event_hash": prev_event_hash or None,
        "mechanical_truth": {"format": "jsonl", "path": jsonl_path.as_posix()},
        "projection": {"format": "markdown+xml", "path": source_path.as_posix()},
    }
    record["event_hash"] = _record_hash(record)
    return record


def _record_hash(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "event_hash"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _append_event_record(path: Path, record: dict[str, Any]) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    encoded = line.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        offset = os.lseek(fd, 0, os.SEEK_END)
        written = os.write(fd, encoded)
        if written != len(encoded):
            raise OSError(f"partial T0 JSONL append: wrote {written} of {len(encoded)} bytes")
        os.fsync(fd)
    finally:
        if locked and fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return offset, len(encoded)


def _append_event_block(
    path: Path,
    *,
    event_id: str,
    sequence: int,
    event_type: str,
    role: str | None,
    content: str,
    created_at: datetime,
    message_id: uuid.UUID | str | None,
    actor_id: uuid.UUID | str | None,
    tenant_id: uuid.UUID | str | None,
    runtime_task_id: uuid.UUID | str | None,
    source: str,
    sensitivity: str,
    metadata: dict[str, Any],
) -> None:
    segment_id = path.parent.name
    session_dir = path.parents[2]
    session_id = session_dir.name
    agent_dir = session_dir.parent.parent.parent.parent
    agent_id = agent_dir.name
    _ensure_segment_file(path, agent_id=agent_id, session_id=session_id, segment_id=segment_id, now=created_at)
    block = _render_event_block(
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        role=role,
        content=content,
        created_at=created_at,
        message_id=message_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        runtime_task_id=runtime_task_id,
        source=source,
        sensitivity=sensitivity,
        metadata=metadata,
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())


def _ensure_segment_file(
    path: Path,
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    segment_id: str,
    now: datetime,
) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# T0 Session Ledger\n\n"
        f"schema_version: {SCHEMA_VERSION}\n"
        f"agent_id: {agent_id}\n"
        f"session_id: {session_id}\n"
        f"segment_id: {segment_id}\n"
        f"created_at: {_iso(now)}\n\n"
    )
    path.write_text(header, encoding="utf-8")


def _render_event_block(
    *,
    event_id: str,
    sequence: int,
    event_type: str,
    role: str | None,
    content: str,
    created_at: datetime,
    message_id: uuid.UUID | str | None,
    actor_id: uuid.UUID | str | None,
    tenant_id: uuid.UUID | str | None,
    runtime_task_id: uuid.UUID | str | None,
    source: str,
    sensitivity: str,
    metadata: dict[str, Any],
) -> str:
    attrs = {
        "id": event_id,
        "seq": str(sequence),
        "event_type": event_type,
        "role": role or "",
        "created_at": _iso(created_at),
        "message_id": _id_value(message_id),
        "actor_id": _id_value(actor_id),
        "tenant_id": _id_value(tenant_id),
        "runtime_task_id": _id_value(runtime_task_id) or _metadata_text(metadata, "runtime_task_id"),
        "turn_id": _metadata_text(metadata, "turn_id"),
        "intent_id": _metadata_text(metadata, "intent_id"),
        "source": source,
        "sensitivity": sensitivity,
    }
    rendered_attrs = " ".join(f"{key}={quoteattr(value)}" for key, value in attrs.items())
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    return (
        f"<t0_event {rendered_attrs}>\n"
        f"  <content>{xml_escape(content)}</content>\n"
        f"  <metadata>{xml_escape(metadata_json)}</metadata>\n"
        "</t0_event>\n\n"
    )


def _parse_events_from_source(*, path: Path, segment_id: str) -> list[T0SessionEvent]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parsed: list[T0SessionEvent] = []
    for match in _EVENT_BLOCK_RE.finditer(text):
        try:
            node = ET.fromstring(match.group(0))
        except ET.ParseError:
            continue
        attrs = dict(node.attrib)
        raw_metadata = node.findtext("metadata") or "{}"
        try:
            metadata = json.loads(raw_metadata)
        except json.JSONDecodeError:
            metadata = {"raw_metadata": raw_metadata}
        if not isinstance(metadata, dict):
            metadata = {"value": metadata}
        parsed.append(
            T0SessionEvent(
                event_id=attrs.get("id", ""),
                sequence=_safe_int(attrs.get("seq")),
                event_type=attrs.get("event_type", ""),
                role=attrs.get("role") or None,
                content=node.findtext("content") or "",
                created_at=attrs.get("created_at", ""),
                message_id=attrs.get("message_id") or None,
                actor_id=attrs.get("actor_id") or None,
                runtime_task_id=attrs.get("runtime_task_id") or None,
                turn_id=attrs.get("turn_id") or None,
                intent_id=attrs.get("intent_id") or None,
                source=attrs.get("source", ""),
                sensitivity=attrs.get("sensitivity", ""),
                metadata=metadata,
                path=path,
                segment_id=segment_id,
                truth_path=path,
                record_schema_version=MARKDOWN_PROJECTION_SCHEMA_VERSION,
            )
        )
    return parsed


def _parse_events_from_jsonl(*, path: Path, segment_id: str, source_path: Path) -> list[T0SessionEvent]:
    parsed: list[T0SessionEvent] = []
    offset = 0
    try:
        with path.open("rb") as fh:
            for raw_line in fh:
                byte_length = len(raw_line)
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    offset += byte_length
                    continue
                if not isinstance(record, dict):
                    offset += byte_length
                    continue
                event = _event_from_record(
                    record=record,
                    jsonl_path=path,
                    source_path=source_path,
                    segment_id=segment_id,
                    byte_offset=offset,
                    byte_length=byte_length,
                )
                if event is not None:
                    parsed.append(event)
                offset += byte_length
    except OSError:
        return []
    return parsed


def _event_from_record(
    *,
    record: dict[str, Any],
    jsonl_path: Path,
    source_path: Path,
    segment_id: str,
    byte_offset: int,
    byte_length: int,
) -> T0SessionEvent | None:
    schema = str(record.get("schema_version") or "")
    if schema != EVENT_RECORD_SCHEMA_VERSION:
        return None
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    projection = record.get("projection") if isinstance(record.get("projection"), dict) else {}
    projected_path = source_path
    projection_path = str(projection.get("path") or "").strip()
    if projection_path:
        session_dir = jsonl_path.parents[2]
        projected_path = session_dir / projection_path
    return T0SessionEvent(
        event_id=str(record.get("event_id") or ""),
        sequence=_safe_int(record.get("sequence")),
        event_type=str(record.get("event_type") or ""),
        role=str(record.get("role")) if record.get("role") else None,
        content=str(record.get("content") or ""),
        created_at=str(record.get("created_at") or ""),
        message_id=str(record.get("message_id")) if record.get("message_id") else None,
        actor_id=str(record.get("actor_id")) if record.get("actor_id") else None,
        runtime_task_id=str(record.get("runtime_task_id")) if record.get("runtime_task_id") else None,
        turn_id=str(record.get("turn_id") or metadata.get("turn_id"))
        if record.get("turn_id") or metadata.get("turn_id")
        else None,
        intent_id=str(record.get("intent_id") or metadata.get("intent_id"))
        if record.get("intent_id") or metadata.get("intent_id")
        else None,
        source=str(record.get("source") or ""),
        sensitivity=str(record.get("sensitivity") or ""),
        metadata=metadata,
        path=projected_path,
        segment_id=str(record.get("segment_id") or segment_id),
        truth_path=jsonl_path,
        byte_offset=byte_offset,
        byte_length=byte_length,
        event_hash=str(record.get("event_hash")) if record.get("event_hash") else None,
        prev_event_hash=str(record.get("prev_event_hash")) if record.get("prev_event_hash") else None,
        record_schema_version=schema,
    )


def _discover_segments(session_dir: Path) -> list[dict[str, Any]]:
    segments_dir = session_dir / "segments"
    if not segments_dir.exists():
        return []
    return [
        {
            "segment_id": segment_dir.name,
            "path": (Path("segments") / segment_dir.name / SOURCE_FILENAME).as_posix(),
            "events_path": (Path("segments") / segment_dir.name / EVENTS_FILENAME).as_posix(),
        }
        for segment_dir in sorted(segments_dir.iterdir())
        if (segment_dir / SOURCE_FILENAME).exists() or (segment_dir / EVENTS_FILENAME).exists()
    ]


def _sanitize_t0_content(content: Any) -> tuple[str, str, list[str]]:
    if isinstance(content, str):
        raw = content
    else:
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True)
    decision = PrivacyLayer(PrivacyStore()).classify_and_mask(raw)
    lint = lint_memory_form(decision.sanitized_text)
    form_warnings = sorted({violation.code for violation in lint.violations if violation.code != "empty"})
    return decision.sanitized_text, decision.sensitivity.value, form_warnings


def _clean_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool, list, dict)):
            cleaned[str(key)] = value
        else:
            cleaned[str(key)] = str(value)
    return cleaned


def _metadata_text(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    return "" if value in (None, "") else str(value)


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _canonical_runtime_task_id(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return uuid.UUID(text).hex
    except ValueError:
        return text


def _record_turn_start_metadata(
    segment: dict[str, Any],
    *,
    event_id: str,
    event_type: str,
    role: str | None,
    metadata: dict[str, Any],
) -> None:
    for key in ("turn_id", "intent_id", "runtime_task_id", "request_id", "trace_id"):
        value = metadata.get(key)
        if value not in (None, ""):
            segment.setdefault(key, value)

    normalized_event_type = str(event_type or "").strip().lower()
    normalized_role = str(role or "").strip().lower()
    if normalized_role == "user" or normalized_event_type in {"user_message", "user_prompt_submit"}:
        segment.setdefault("start_event_id", event_id)
        segment["user_prompt_submit_event_id"] = str(metadata.get("user_prompt_submit_event_id") or event_id)


def _boundary_metadata(*, reason: str, event_id: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
    cleaned = _clean_metadata(metadata)
    event_metadata = {"reason": reason, **cleaned, "boundary_event_id": event_id}
    checkpoint_kind = str(event_metadata.get("checkpoint_kind") or "").strip()
    if checkpoint_kind == "user_turn_stop":
        event_metadata["turn_stop_event_id"] = event_id
    elif checkpoint_kind in {"turn_abort", "stop_failure", "turn_failure"}:
        event_metadata["turn_abort_event_id"] = event_id
    return event_metadata


def _record_turn_boundary_metadata(segment: dict[str, Any], *, event_id: str, metadata: dict[str, Any]) -> None:
    segment["boundary_event_id"] = event_id
    for key in (
        "turn_id",
        "intent_id",
        "checkpoint_kind",
        "turn_stop_event_id",
        "turn_abort_event_id",
        "user_prompt_submit_event_id",
        "runtime_task_id",
        "request_id",
        "trace_id",
    ):
        value = metadata.get(key)
        if value not in (None, ""):
            segment[key] = value


def _utc_now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


def _id_value(value: uuid.UUID | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, uuid.UUID):
        return value.hex
    return str(value)
