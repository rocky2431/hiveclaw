"""Durable caller-owned terminal boundary delivery and reconciliation.

The outbox carries no model-authored bytes.  A claimed item contains only a
hash-pinned authority binding; the injected validator must reconstruct that
binding from canonical rows before the injected processor may run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from typing import Any
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, tenant_scoped_session
from app.models.runtime_task import (
    TERMINAL_BOUNDARY_RETRY_SECONDS,
    TERMINAL_BOUNDARY_TERMINAL_STATUSES,
    RuntimeTask,
)
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox


_OUTBOX_ID_NAMESPACE = uuid.UUID("225a2a84-4d5c-5be2-b0b7-7aca21a8a18b")
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_T0_EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{32}$")
_SOURCE_REF_RE = re.compile(r"^(?P<scheme>[a-z][a-z0-9-]{0,63})://(?P<identifier>[^/]{1,64})(?P<suffix>(?:/[0-9]+)?)$")
_MAX_BINDING_BYTES = 64 * 1024
_MAX_BINDING_DEPTH = 5
_MAX_CONTAINER_ITEMS = 256

_IDENTIFIER_KEYS = frozenset(
    {
        "id",
        "tenant_id",
        "runtime_task_id",
        "task_id",
        "parent_task_id",
        "child_task_id",
        "agent_id",
        "authority_id",
        "outcome_id",
        "result_id",
        "terminal_result_id",
        "event_id",
        "assistant_final_event_id",
        "terminal_event_id",
        "response_event_id",
        "abort_event_id",
        "boundary_id",
        "terminal_boundary_id",
        "t0_boundary_id",
        "run_id",
        "trigger_id",
        "trigger_run_id",
        "delegation_id",
        "delegation_run_id",
        "heartbeat_id",
        "heartbeat_run_id",
        "execution_id",
        "input_id",
        "message_id",
    }
)
_SEQUENCE_KEYS = frozenset(
    {
        "sequence",
        "event_sequence",
        "assistant_final_sequence",
        "terminal_sequence",
        "response_sequence",
        "boundary_sequence",
        "summary_sequence",
        "t0_sequence",
    }
)
_SHA256_KEYS = frozenset(
    {
        "sha256",
        "authority_sha256",
        "model_request_sha256",
        "result_content_sha256",
        "semantic_content_sha256",
        "assistant_final_sha256",
        "terminal_event_sha256",
        "response_projection_sha256",
        "direct_projection_sha256",
    }
)
_SOURCE_REF_KEYS = frozenset({"source_ref", "summary_source_ref"})
_SOURCE_REF_LIST_KEYS = frozenset({"source_refs"})
_NAME_KEYS = frozenset({"authority_ref"})
_T0_EVENT_ID_KEYS = frozenset({"t0_event_id"})
_SESSION_ID_KEYS = frozenset({"session_id"})
_SOURCE_REF_SCHEMES = frozenset(
    {
        "runtime-terminal-boundary",
        "runtime-task",
        "session-event",
        "session-run-outcome",
        "session-model-result",
        "chat-session-summary",
        "chat-session-summary-skipped",
        "chat-session-summary-superseded",
    }
)


class TerminalBoundaryError(ValueError):
    """Base class carrying a safe machine-readable failure code."""

    code = "terminal_boundary_error"


class TerminalBoundaryBindingError(TerminalBoundaryError):
    code = "terminal_boundary_binding_invalid"


class TerminalBoundaryIdempotencyConflict(TerminalBoundaryError):
    code = "terminal_boundary_idempotency_conflict"


class TerminalBoundaryCanonicalMismatch(TerminalBoundaryError):
    code = "terminal_boundary_canonical_mismatch"


class StaleTerminalBoundaryClaim(TerminalBoundaryError):
    code = "terminal_boundary_claim_stale"


def _binding_key_kind(key: str) -> str | None:
    if key in _SEQUENCE_KEYS:
        return "sequence"
    if key in _SHA256_KEYS:
        return "sha256"
    if key in _IDENTIFIER_KEYS:
        return "id"
    if key in _SOURCE_REF_KEYS:
        return "ref"
    if key in _SOURCE_REF_LIST_KEYS:
        return "refs"
    if key in _NAME_KEYS:
        return "name"
    if key in _T0_EVENT_ID_KEYS:
        return "t0_event_id"
    if key in _SESSION_ID_KEYS:
        return "session_id"
    return None


def _normalize_identifier(value: Any, *, path: str) -> str:
    if isinstance(value, uuid.UUID):
        normalized = str(value)
    elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        normalized = str(value)
    elif isinstance(value, str):
        normalized = value.strip()
        try:
            normalized = str(uuid.UUID(normalized))
        except ValueError:
            if not normalized.isdecimal():
                raise TerminalBoundaryBindingError(f"{path} must be a UUID or non-negative integer identifier")
            normalized = str(int(normalized))
    else:
        raise TerminalBoundaryBindingError(f"{path} must be a UUID or non-negative integer identifier")
    return normalized


def _normalize_source_ref(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise TerminalBoundaryBindingError(f"{path} must be a canonical source reference")
    match = _SOURCE_REF_RE.fullmatch(value.strip())
    if match is None or match.group("scheme") not in _SOURCE_REF_SCHEMES:
        raise TerminalBoundaryBindingError(f"{path} uses an unsupported source reference scheme")
    identifier = _normalize_identifier(match.group("identifier"), path=f"{path}.identifier")
    return f"{match.group('scheme')}://{identifier}{match.group('suffix')}"


def _normalize_t0_event_id(value: Any, *, path: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _T0_EVENT_ID_RE.fullmatch(normalized):
        raise TerminalBoundaryBindingError(f"{path} must be a canonical T0 event identifier")
    return normalized


def _normalize_session_identifier(value: Any, *, path: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_SESSION_ID_RE.fullmatch(normalized):
        raise TerminalBoundaryBindingError(f"{path} must be a safe runtime session identifier")
    return normalized


def _normalize_sequence(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TerminalBoundaryBindingError(f"{path} must be a non-negative integer sequence")
    return value


def _normalize_sha256(value: Any, *, path: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise TerminalBoundaryBindingError(f"{path} must be a 64-character sha256")
    return normalized


def _normalize_binding_mapping(value: Mapping[str, Any], *, path: str, depth: int) -> dict[str, Any]:
    if depth > _MAX_BINDING_DEPTH:
        raise TerminalBoundaryBindingError(f"{path} exceeds the maximum binding depth")
    if len(value) > _MAX_CONTAINER_ITEMS:
        raise TerminalBoundaryBindingError(f"{path} contains too many binding items")
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in sorted(value.items(), key=lambda pair: str(pair[0])):
        key = str(raw_key)
        kind = _binding_key_kind(key)
        child_path = f"{path}.{key}"
        if kind is None:
            raise TerminalBoundaryBindingError(
                f"{child_path} is forbidden; bindings accept only IDs, sequences, sha256 hashes, and source refs"
            )
        if kind == "id":
            normalized[key] = _normalize_identifier(raw_value, path=child_path)
        elif kind == "ref":
            normalized[key] = _normalize_source_ref(raw_value, path=child_path)
        elif kind == "name":
            normalized[key] = _normalize_name(raw_value, field=child_path)
        elif kind == "t0_event_id":
            normalized[key] = _normalize_t0_event_id(raw_value, path=child_path)
        elif kind == "session_id":
            normalized[key] = _normalize_session_identifier(raw_value, path=child_path)
        elif kind == "sequence":
            normalized[key] = _normalize_sequence(raw_value, path=child_path)
        elif kind == "sha256":
            normalized[key] = _normalize_sha256(raw_value, path=child_path)
        else:
            if not isinstance(raw_value, (list, tuple)):
                raise TerminalBoundaryBindingError(f"{child_path} must be a list")
            if len(raw_value) > _MAX_CONTAINER_ITEMS:
                raise TerminalBoundaryBindingError(f"{child_path} contains too many binding items")
            items: list[Any] = []
            for index, item in enumerate(raw_value):
                item_path = f"{child_path}[{index}]"
                if isinstance(item, Mapping):
                    items.append(_normalize_binding_mapping(item, path=item_path, depth=depth + 1))
                else:
                    items.append(_normalize_source_ref(item, path=item_path))
            normalized[key] = items
    return normalized


def normalize_terminal_boundary_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the content-free binding contract or fail closed."""

    if not isinstance(binding, Mapping):
        raise TerminalBoundaryBindingError("binding must be a mapping")
    normalized = _normalize_binding_mapping(binding, path="binding", depth=0)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded) > _MAX_BINDING_BYTES:
        raise TerminalBoundaryBindingError("binding exceeds the 64 KiB authority-reference limit")
    return normalized


def terminal_boundary_binding_sha256(binding: Mapping[str, Any]) -> str:
    normalized = normalize_terminal_boundary_binding(binding)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_name(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SAFE_NAME_RE.fullmatch(normalized):
        raise TerminalBoundaryBindingError(f"{field} must be a stable machine name")
    return normalized


def _uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise TerminalBoundaryBindingError(f"{field} must be a UUID") from exc


def terminal_boundary_idempotency_key(
    *,
    tenant_id: uuid.UUID,
    event_kind: str,
    authority_ref: str,
    authority_id: str | uuid.UUID,
) -> str:
    material = {
        "authority_id": _normalize_identifier(authority_id, path="authority_id"),
        "authority_ref": _normalize_name(authority_ref, field="authority_ref"),
        "event_kind": _normalize_name(event_kind, field="event_kind"),
        "tenant_id": str(_uuid(tenant_id, field="tenant_id")),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def terminal_boundary_outbox_id(
    *,
    tenant_id: uuid.UUID,
    event_kind: str,
    authority_ref: str,
    authority_id: str | uuid.UUID,
) -> uuid.UUID:
    key = terminal_boundary_idempotency_key(
        tenant_id=tenant_id,
        event_kind=event_kind,
        authority_ref=authority_ref,
        authority_id=authority_id,
    )
    return uuid.uuid5(_OUTBOX_ID_NAMESPACE, key)


async def enqueue_terminal_boundary(
    db: AsyncSession,
    *,
    task: RuntimeTask,
    event_kind: str,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    terminal_status: str,
    authority_ref: str,
    authority_id: uuid.UUID | str,
    binding: Mapping[str, Any],
) -> RuntimeTerminalBoundaryOutbox:
    """Enqueue one content-free boundary in the caller's terminal transaction."""

    tenant_id = _uuid(task.tenant_id, field="task.tenant_id")
    task_id = _uuid(task.id, field="task.id")
    normalized_agent_id = _uuid(agent_id, field="agent_id")
    normalized_session_id = _normalize_session_identifier(session_id, path="session_id")
    normalized_event_kind = _normalize_name(event_kind, field="event_kind")
    normalized_terminal_status = _normalize_name(terminal_status, field="terminal_status")
    normalized_authority_ref = _normalize_name(authority_ref, field="authority_ref")
    normalized_authority_id = _normalize_identifier(authority_id, path="authority_id")

    if normalized_terminal_status != str(task.status or "").strip().lower():
        raise TerminalBoundaryBindingError("terminal_status must match the canonical RuntimeTask status")
    if normalized_terminal_status not in TERMINAL_BOUNDARY_TERMINAL_STATUSES:
        raise TerminalBoundaryBindingError("RuntimeTask is not in a terminal status")
    if task.terminal_boundary_generation is None:
        raise TerminalBoundaryBindingError("RuntimeTask is outside the terminal-boundary cutover generation")

    required_binding = normalize_terminal_boundary_binding(
        {
            "tenant_id": tenant_id,
            "runtime_task_id": task_id,
            "agent_id": normalized_agent_id,
            "session_id": normalized_session_id,
            "authority_ref": normalized_authority_ref,
            "authority_id": normalized_authority_id,
        }
    )
    normalized_binding = normalize_terminal_boundary_binding(binding)
    for key, expected in required_binding.items():
        if key in normalized_binding and normalized_binding[key] != expected:
            raise TerminalBoundaryBindingError(f"binding.{key} conflicts with the authoritative enqueue argument")
    normalized_binding = normalize_terminal_boundary_binding({**normalized_binding, **required_binding})
    binding_sha256 = terminal_boundary_binding_sha256(normalized_binding)
    idempotency_key = terminal_boundary_idempotency_key(
        tenant_id=tenant_id,
        event_kind=normalized_event_kind,
        authority_ref=normalized_authority_ref,
        authority_id=normalized_authority_id,
    )
    outbox_id = terminal_boundary_outbox_id(
        tenant_id=tenant_id,
        event_kind=normalized_event_kind,
        authority_ref=normalized_authority_ref,
        authority_id=normalized_authority_id,
    )
    now = datetime.now(UTC)
    statement = (
        insert(RuntimeTerminalBoundaryOutbox)
        .values(
            id=outbox_id,
            tenant_id=tenant_id,
            runtime_task_id=task_id,
            agent_id=normalized_agent_id,
            session_id=normalized_session_id,
            event_kind=normalized_event_kind,
            terminal_status=normalized_terminal_status,
            authority_ref=normalized_authority_ref,
            authority_id=normalized_authority_id,
            binding_json=normalized_binding,
            binding_sha256=binding_sha256,
            idempotency_key=idempotency_key,
            status="pending",
            attempt_count=0,
            available_at=now,
        )
        .on_conflict_do_nothing()
    )
    await db.execute(statement)
    row = (
        await db.execute(
            select(RuntimeTerminalBoundaryOutbox)
            .where(
                RuntimeTerminalBoundaryOutbox.id == outbox_id,
                RuntimeTerminalBoundaryOutbox.tenant_id == tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one()
    accepted = (
        row.runtime_task_id == task_id
        and row.agent_id == normalized_agent_id
        and row.session_id == normalized_session_id
        and row.event_kind == normalized_event_kind
        and row.terminal_status == normalized_terminal_status
        and row.authority_ref == normalized_authority_ref
        and row.authority_id == normalized_authority_id
        and row.binding_sha256 == binding_sha256
        and dict(row.binding_json or {}) == normalized_binding
        and row.idempotency_key == idempotency_key
    )
    if not accepted:
        raise TerminalBoundaryIdempotencyConflict(
            "the stable terminal boundary identity is already bound to different canonical facts"
        )
    if task.terminal_boundary_enqueued_at is None:
        task.terminal_boundary_enqueued_at = now
    task.terminal_boundary_reconcile_last_error = None
    await db.flush()
    return row


@dataclass(frozen=True, slots=True)
class ClaimedTerminalBoundary:
    id: uuid.UUID
    tenant_id: uuid.UUID
    runtime_task_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: str
    event_kind: str
    terminal_status: str
    authority_ref: str
    authority_id: str
    binding: dict[str, Any]
    binding_sha256: str
    idempotency_key: str
    claim_token: uuid.UUID
    attempt: int


CanonicalBindingValidator = Callable[[AsyncSession, ClaimedTerminalBoundary], Awaitable[Mapping[str, Any]]]
TerminalBoundaryProcessor = Callable[[ClaimedTerminalBoundary], Awaitable[Mapping[str, Any] | None]]
TerminalBoundaryBuilder = Callable[
    [AsyncSession, RuntimeTask],
    Awaitable[Sequence[RuntimeTerminalBoundaryOutbox] | None],
]


async def enqueue_required_terminal_boundary_for_task(
    db: AsyncSession,
    task: RuntimeTask,
) -> RuntimeTerminalBoundaryOutbox | None:
    """Route one terminal RuntimeTask to its canonical boundary builder."""

    if task.terminal_boundary_generation is None:
        return None
    from app.services.web_chat_runtime import EXECUTABLE_CHAT_TASK_TYPES

    if task.task_type in EXECUTABLE_CHAT_TASK_TYPES:
        from app.services.web_terminal_boundary_processor import enqueue_web_terminal_boundary_for_task

        row = await enqueue_web_terminal_boundary_for_task(db, task)
    else:
        from app.services.direct_invocation_terminal_boundary_processor import (
            DIRECT_INVOCATION_TASK_TYPES,
            enqueue_direct_terminal_boundary_for_task,
        )

        if task.task_type not in DIRECT_INVOCATION_TASK_TYPES:
            raise TerminalBoundaryBindingError("terminal RuntimeTask has no boundary processor")
        row = await enqueue_direct_terminal_boundary_for_task(db, task)
    if row is None and task.terminal_boundary_enqueued_at is None:
        raise TerminalBoundaryCanonicalMismatch("terminal RuntimeTask boundary was not enqueued")
    return row


def _safe_error(error: Exception) -> str:
    if isinstance(error, TerminalBoundaryError):
        return error.code
    return type(error).__name__[:100]


class RuntimeTerminalBoundaryOutboxService:
    """Tenant-scoped claim, validate, process, retry, and reconcile worker."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 2,
        max_attempts: int = 8,
        reconcile_retry_seconds: int = TERMINAL_BOUNDARY_RETRY_SECONDS,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._lease_seconds = max(1, int(lease_seconds))
        self._retry_base_seconds = max(0, int(retry_base_seconds))
        self._max_attempts = max(1, int(max_attempts))
        self._reconcile_retry_seconds = max(0, int(reconcile_retry_seconds))

    def _tenant_session(self, tenant_id: uuid.UUID, *, operation: str):
        return tenant_scoped_session(
            tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source=f"runtime_terminal_boundary_outbox.{operation}",
        )

    @staticmethod
    def _claimed(row: RuntimeTerminalBoundaryOutbox) -> ClaimedTerminalBoundary:
        if row.claim_token is None:
            raise StaleTerminalBoundaryClaim("claimed row has no claim token")
        return ClaimedTerminalBoundary(
            id=row.id,
            tenant_id=row.tenant_id,
            runtime_task_id=row.runtime_task_id,
            agent_id=row.agent_id,
            session_id=row.session_id,
            event_kind=row.event_kind,
            terminal_status=row.terminal_status,
            authority_ref=row.authority_ref,
            authority_id=row.authority_id,
            binding=dict(row.binding_json or {}),
            binding_sha256=row.binding_sha256,
            idempotency_key=row.idempotency_key,
            claim_token=row.claim_token,
            attempt=int(row.attempt_count or 0),
        )

    @staticmethod
    def _validate_claim_integrity(item: ClaimedTerminalBoundary) -> None:
        normalized = normalize_terminal_boundary_binding(item.binding)
        if terminal_boundary_binding_sha256(normalized) != item.binding_sha256:
            raise TerminalBoundaryCanonicalMismatch("stored terminal boundary binding hash does not match")
        expected = normalize_terminal_boundary_binding(
            {
                "tenant_id": item.tenant_id,
                "runtime_task_id": item.runtime_task_id,
                "agent_id": item.agent_id,
                "session_id": item.session_id,
                "authority_ref": item.authority_ref,
                "authority_id": item.authority_id,
            }
        )
        if any(normalized.get(key) != value for key, value in expected.items()):
            raise TerminalBoundaryCanonicalMismatch("stored terminal boundary columns and binding disagree")
        expected_key = terminal_boundary_idempotency_key(
            tenant_id=item.tenant_id,
            event_kind=item.event_kind,
            authority_ref=item.authority_ref,
            authority_id=item.authority_id,
        )
        if item.idempotency_key != expected_key or item.id != uuid.uuid5(_OUTBOX_ID_NAMESPACE, expected_key):
            raise TerminalBoundaryCanonicalMismatch("stored terminal boundary stable identity disagrees")

    async def claim_batch(
        self,
        *,
        tenant_id: uuid.UUID | str,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 100,
        task_types: Sequence[str] | None = None,
    ) -> list[ClaimedTerminalBoundary]:
        """Claim one boundary that can be fenced immediately.

        Long-running required consumers must never sit behind another item on
        an expiring lease.  Throughput comes from independent workers, each
        holding one claim, rather than preclaiming a batch.
        """
        tenant_uuid = _uuid(tenant_id, field="tenant_id")
        normalized_worker = str(worker_id or "").strip()
        if not normalized_worker or len(normalized_worker) > 200:
            raise ValueError("worker_id must contain 1-200 characters")
        requested_limit = max(1, int(limit))
        current = now or datetime.now(UTC)
        statement = select(RuntimeTerminalBoundaryOutbox)
        if task_types:
            statement = statement.join(
                RuntimeTask,
                and_(
                    RuntimeTask.id == RuntimeTerminalBoundaryOutbox.runtime_task_id,
                    RuntimeTask.tenant_id == RuntimeTerminalBoundaryOutbox.tenant_id,
                    RuntimeTask.task_type.in_(tuple(task_types)),
                ),
            )
        statement = (
            statement.where(
                RuntimeTerminalBoundaryOutbox.tenant_id == tenant_uuid,
                or_(
                    and_(
                        RuntimeTerminalBoundaryOutbox.status == "pending",
                        RuntimeTerminalBoundaryOutbox.available_at <= current,
                    ),
                    and_(
                        RuntimeTerminalBoundaryOutbox.status == "processing",
                        or_(
                            RuntimeTerminalBoundaryOutbox.lease_expires_at.is_(None),
                            RuntimeTerminalBoundaryOutbox.lease_expires_at <= current,
                        ),
                    ),
                ),
            )
            .order_by(
                RuntimeTerminalBoundaryOutbox.available_at,
                RuntimeTerminalBoundaryOutbox.created_at,
            )
            .limit(min(requested_limit, 1))
            .with_for_update(skip_locked=True)
        )
        async with self._tenant_session(tenant_uuid, operation="claim") as db:
            rows = list((await db.execute(statement)).scalars())
            claimed: list[ClaimedTerminalBoundary] = []
            for row in rows:
                row.status = "processing"
                row.claimed_by = normalized_worker
                row.claim_token = uuid.uuid4()
                row.lease_expires_at = current + timedelta(seconds=self._lease_seconds)
                row.attempt_count = int(row.attempt_count or 0) + 1
                claimed.append(self._claimed(row))
            await db.flush()
            return claimed

    async def _validate_canonical_binding(
        self,
        *,
        db: AsyncSession,
        item: ClaimedTerminalBoundary,
        worker_id: str,
        canonical_validator: CanonicalBindingValidator,
    ) -> RuntimeTerminalBoundaryOutbox:
        current = datetime.now(UTC)
        row = await db.scalar(
            select(RuntimeTerminalBoundaryOutbox)
            .where(
                RuntimeTerminalBoundaryOutbox.id == item.id,
                RuntimeTerminalBoundaryOutbox.tenant_id == item.tenant_id,
                RuntimeTerminalBoundaryOutbox.status == "processing",
                RuntimeTerminalBoundaryOutbox.claimed_by == str(worker_id),
                RuntimeTerminalBoundaryOutbox.claim_token == item.claim_token,
                RuntimeTerminalBoundaryOutbox.lease_expires_at.is_not(None),
                RuntimeTerminalBoundaryOutbox.lease_expires_at > current,
            )
            .with_for_update()
        )
        if row is None:
            raise StaleTerminalBoundaryClaim("terminal boundary claim is no longer current")
        row.lease_expires_at = current + timedelta(seconds=self._lease_seconds)
        await db.flush()
        canonical = normalize_terminal_boundary_binding(await canonical_validator(db, item))
        if canonical != normalize_terminal_boundary_binding(item.binding):
            raise TerminalBoundaryCanonicalMismatch("canonical authority binding does not match claimed binding")
        return row

    async def renew_terminal_boundary_claim(
        self,
        *,
        item: ClaimedTerminalBoundary,
        worker_id: str,
    ) -> bool:
        """Renew only the still-live exact claim; an expired fence stays expired."""

        current = datetime.now(UTC)
        async with self._tenant_session(item.tenant_id, operation="renew") as db:
            row = await db.scalar(
                select(RuntimeTerminalBoundaryOutbox)
                .where(
                    RuntimeTerminalBoundaryOutbox.id == item.id,
                    RuntimeTerminalBoundaryOutbox.tenant_id == item.tenant_id,
                    RuntimeTerminalBoundaryOutbox.status == "processing",
                    RuntimeTerminalBoundaryOutbox.claimed_by == str(worker_id),
                    RuntimeTerminalBoundaryOutbox.claim_token == item.claim_token,
                    RuntimeTerminalBoundaryOutbox.lease_expires_at.is_not(None),
                    RuntimeTerminalBoundaryOutbox.lease_expires_at > current,
                )
                .with_for_update()
            )
            if row is None:
                return False
            row.lease_expires_at = current + timedelta(seconds=self._lease_seconds)
            await db.flush()
            return True

    async def _renew_terminal_boundary_claim_until_cancelled(
        self,
        *,
        item: ClaimedTerminalBoundary,
        worker_id: str,
    ) -> None:
        interval = max(0.05, float(self._lease_seconds) / 3)
        while True:
            await asyncio.sleep(interval)
            if not await self.renew_terminal_boundary_claim(item=item, worker_id=worker_id):
                raise StaleTerminalBoundaryClaim("terminal boundary claim renewal lost its fence")

    async def ack_terminal_boundary(
        self,
        *,
        item: ClaimedTerminalBoundary,
        worker_id: str,
        receipt: Mapping[str, Any] | None = None,
    ) -> bool:
        normalized_receipt = normalize_terminal_boundary_binding(receipt or {}) if receipt is not None else None
        async with self._tenant_session(item.tenant_id, operation="ack") as db:
            row = await db.scalar(
                select(RuntimeTerminalBoundaryOutbox)
                .where(
                    RuntimeTerminalBoundaryOutbox.id == item.id,
                    RuntimeTerminalBoundaryOutbox.tenant_id == item.tenant_id,
                )
                .with_for_update()
            )
            if (
                row is None
                or row.status != "processing"
                or row.claimed_by != str(worker_id)
                or row.claim_token != item.claim_token
            ):
                return False
            row.status = "delivered"
            row.delivery_receipt_json = normalized_receipt
            row.delivered_at = datetime.now(UTC)
            row.last_error = None
            row.claimed_by = None
            row.claim_token = None
            row.lease_expires_at = None
            await db.flush()
            return True

    async def fail_terminal_boundary(
        self,
        *,
        item: ClaimedTerminalBoundary,
        worker_id: str,
        error: Exception,
    ) -> str:
        now = datetime.now(UTC)
        async with self._tenant_session(item.tenant_id, operation="fail") as db:
            row = await db.scalar(
                select(RuntimeTerminalBoundaryOutbox)
                .where(
                    RuntimeTerminalBoundaryOutbox.id == item.id,
                    RuntimeTerminalBoundaryOutbox.tenant_id == item.tenant_id,
                )
                .with_for_update()
            )
            if (
                row is None
                or row.status != "processing"
                or row.claimed_by != str(worker_id)
                or row.claim_token != item.claim_token
            ):
                return "stale"
            row.last_error = _safe_error(error)
            row.claimed_by = None
            row.claim_token = None
            row.lease_expires_at = None
            if int(row.attempt_count or 0) >= self._max_attempts:
                row.status = "dead_letter"
                outcome = "dead_letter"
            else:
                row.status = "pending"
                delay = self._retry_base_seconds * (2 ** max(0, int(row.attempt_count or 1) - 1))
                row.available_at = now + timedelta(seconds=delay)
                outcome = "retry"
            await db.flush()
            return outcome

    async def redrive_dead_letter(
        self,
        *,
        tenant_id: uuid.UUID | str,
        outbox_id: uuid.UUID | str,
        actor_user_id: uuid.UUID | str,
        reason: str,
        summary_disposition: str | None = None,
    ) -> RuntimeTerminalBoundaryOutbox:
        """Requeue one exact dead letter while preserving failure evidence."""
        from app.models.audit import AuditLog

        tenant_uuid = _uuid(tenant_id, field="tenant_id")
        outbox_uuid = _uuid(outbox_id, field="outbox_id")
        actor_uuid = _uuid(actor_user_id, field="actor_user_id")
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 1000:
            raise ValueError("dead-letter redrive reason must contain 1-1000 characters")
        normalized_summary_disposition = str(summary_disposition or "").strip().lower() or None
        if normalized_summary_disposition not in {None, "retry"}:
            raise ValueError("unsupported summary disposition")
        now = datetime.now(UTC)
        async with self._tenant_session(tenant_uuid, operation="redrive") as db:
            row = await db.scalar(
                select(RuntimeTerminalBoundaryOutbox)
                .where(
                    RuntimeTerminalBoundaryOutbox.id == outbox_uuid,
                    RuntimeTerminalBoundaryOutbox.tenant_id == tenant_uuid,
                )
                .with_for_update()
            )
            if row is None:
                raise LookupError("runtime terminal boundary outbox item not found")
            if row.status != "dead_letter":
                raise ValueError("only a dead-letter terminal boundary can be redriven")
            previous_attempt_count = int(row.attempt_count or 0)
            previous_error = row.last_error
            task = await db.scalar(
                select(RuntimeTask)
                .where(
                    RuntimeTask.id == row.runtime_task_id,
                    RuntimeTask.tenant_id == tenant_uuid,
                )
                .with_for_update()
            )
            if task is None:
                raise ValueError("runtime terminal boundary task authority is missing")
            from app.services.web_chat_runtime import EXECUTABLE_CHAT_TASK_TYPES

            summary_reconciliation = None
            is_web_turn_stop = task.task_type in EXECUTABLE_CHAT_TASK_TYPES and row.event_kind == "turn_stop"
            if is_web_turn_stop:
                from app.services.web_terminal_boundary_processor import prepare_web_summary_retry

                summary_reconciliation = await prepare_web_summary_retry(
                    db,
                    tenant_id=tenant_uuid,
                    runtime_task_id=row.runtime_task_id,
                    agent_id=row.agent_id,
                    session_id=row.session_id,
                    terminal_status=row.terminal_status,
                    binding=dict(row.binding_json or {}),
                    disposition=normalized_summary_disposition,
                    actor_user_id=actor_uuid,
                )
            elif normalized_summary_disposition is not None:
                raise ValueError("summary disposition is only valid for a Web turn_stop dead letter")
            db.add(
                AuditLog(
                    tenant_id=tenant_uuid,
                    user_id=actor_uuid,
                    agent_id=row.agent_id,
                    action="runtime_terminal_boundary_redriven",
                    details={
                        "outbox_id": str(row.id),
                        "runtime_task_id": str(row.runtime_task_id),
                        "reason": normalized_reason,
                        "previous_status": "dead_letter",
                        "previous_attempt_count": previous_attempt_count,
                        "previous_error": previous_error,
                        "summary_disposition": normalized_summary_disposition,
                        "summary_reconciliation": summary_reconciliation,
                        "redriven_at": now.isoformat(),
                    },
                )
            )
            row.status = "pending"
            row.available_at = now
            row.claimed_by = None
            row.claim_token = None
            row.lease_expires_at = None
            await db.flush()
            # The UPDATE expires the server-generated ``updated_at``; refresh it
            # while the session is open so the returned row stays readable after
            # the caller's transaction commits and the session closes.
            await db.refresh(row)
            return row

    async def process_terminal_boundary(
        self,
        *,
        item: ClaimedTerminalBoundary,
        worker_id: str,
        canonical_validator: CanonicalBindingValidator,
        process_callback: TerminalBoundaryProcessor,
    ) -> bool:
        self._validate_claim_integrity(item)
        async with self._tenant_session(item.tenant_id, operation="process") as db:
            await self._validate_canonical_binding(
                db=db,
                item=item,
                worker_id=worker_id,
                canonical_validator=canonical_validator,
            )
        callback_task = asyncio.create_task(
            process_callback(item),
            name=f"terminal-boundary-callback:{item.id}",
        )
        renew_task = asyncio.create_task(
            self._renew_terminal_boundary_claim_until_cancelled(item=item, worker_id=worker_id),
            name=f"terminal-boundary-renew:{item.id}",
        )
        try:
            done, _pending = await asyncio.wait(
                (callback_task, renew_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renew_task in done:
                renewal_error = renew_task.exception()
                if renewal_error is not None:
                    callback_task.cancel()
                    await asyncio.gather(callback_task, return_exceptions=True)
                    raise renewal_error
            receipt = await callback_task
            renew_task.cancel()
            await asyncio.gather(renew_task, return_exceptions=True)
            if receipt is not None and not isinstance(receipt, Mapping):
                raise TerminalBoundaryBindingError("processor receipt must be an ID/ref/hash-only mapping")
            if not await self.ack_terminal_boundary(
                item=item,
                worker_id=worker_id,
                receipt=receipt,
            ):
                raise StaleTerminalBoundaryClaim("terminal boundary claim was lost before acknowledgement")
            return True
        finally:
            for task in (callback_task, renew_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(callback_task, renew_task, return_exceptions=True)

    async def drain_once(
        self,
        *,
        tenant_id: uuid.UUID | str,
        worker_id: str,
        canonical_validator: CanonicalBindingValidator,
        process_callback: TerminalBoundaryProcessor,
        limit: int = 100,
        task_types: Sequence[str] | None = None,
    ) -> dict[str, int]:
        items = await self.claim_batch(
            tenant_id=tenant_id,
            worker_id=worker_id,
            limit=limit,
            task_types=task_types,
        )
        counts = {"claimed": len(items), "delivered": 0, "retried": 0, "dead_lettered": 0}
        for item in items:
            try:
                if await self.process_terminal_boundary(
                    item=item,
                    worker_id=worker_id,
                    canonical_validator=canonical_validator,
                    process_callback=process_callback,
                ):
                    counts["delivered"] += 1
            except Exception as exc:  # noqa: BLE001 - durable retry owns callback/validation failures.
                outcome = await self.fail_terminal_boundary(
                    item=item,
                    worker_id=worker_id,
                    error=exc,
                )
                if outcome == "retry":
                    counts["retried"] += 1
                elif outcome == "dead_letter":
                    counts["dead_lettered"] += 1
        return counts

    async def _claim_terminal_tasks_for_reconcile(
        self,
        *,
        tenant_id: uuid.UUID,
        now: datetime,
        limit: int,
        task_types: Sequence[str] | None = None,
    ) -> list[uuid.UUID]:
        retry_before = now - timedelta(seconds=self._reconcile_retry_seconds)
        filters = [
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.terminal_boundary_generation.is_not(None),
            RuntimeTask.terminal_boundary_enqueued_at.is_(None),
            RuntimeTask.status.in_(TERMINAL_BOUNDARY_TERMINAL_STATUSES),
            or_(
                RuntimeTask.terminal_boundary_reconcile_attempted_at.is_(None),
                RuntimeTask.terminal_boundary_reconcile_attempted_at <= retry_before,
            ),
        ]
        if task_types:
            filters.append(RuntimeTask.task_type.in_(tuple(task_types)))
        async with self._tenant_session(tenant_id, operation="reconcile_claim") as db:
            tasks = list(
                (
                    await db.execute(
                        select(RuntimeTask)
                        .where(*filters)
                        .order_by(
                            RuntimeTask.terminal_boundary_reconcile_attempted_at.asc().nullsfirst(),
                            RuntimeTask.created_at,
                        )
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for task in tasks:
                task.terminal_boundary_reconcile_attempted_at = now
                task.terminal_boundary_reconcile_attempt_count = (
                    int(task.terminal_boundary_reconcile_attempt_count or 0) + 1
                )
                task.terminal_boundary_reconcile_last_error = None
            await db.flush()
            return [task.id for task in tasks]

    async def _record_reconcile_failure(
        self,
        *,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID,
        error: Exception,
    ) -> None:
        async with self._tenant_session(tenant_id, operation="reconcile_hold") as db:
            task = await db.scalar(
                select(RuntimeTask)
                .where(RuntimeTask.id == task_id, RuntimeTask.tenant_id == tenant_id)
                .with_for_update()
            )
            if task is not None and task.terminal_boundary_enqueued_at is None:
                task.terminal_boundary_reconcile_last_error = _safe_error(error)
                await db.flush()

    async def reconcile_terminal_tasks_once(
        self,
        *,
        tenant_id: uuid.UUID | str,
        builder: TerminalBoundaryBuilder,
        limit: int = 100,
        now: datetime | None = None,
        task_types: Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Recover cutover-eligible terminal tasks missing their boundary row.

        ``builder`` runs in one tenant transaction and must use
        :func:`enqueue_terminal_boundary`; any exception rolls back every row
        it attempted for that task.  Historical terminal tasks have NULL
        generation and never enter this recovery lane.
        """

        tenant_uuid = _uuid(tenant_id, field="tenant_id")
        current = now or datetime.now(UTC)
        task_ids = await self._claim_terminal_tasks_for_reconcile(
            tenant_id=tenant_uuid,
            now=current,
            limit=limit,
            task_types=task_types,
        )
        counts = {"claimed": len(task_ids), "enqueued": 0, "held": 0}
        for task_id in task_ids:
            try:
                async with self._tenant_session(tenant_uuid, operation="reconcile_build") as db:
                    task = await db.scalar(
                        select(RuntimeTask)
                        .where(RuntimeTask.id == task_id, RuntimeTask.tenant_id == tenant_uuid)
                        .with_for_update()
                    )
                    if task is None:
                        raise TerminalBoundaryCanonicalMismatch("reconcile RuntimeTask no longer exists")
                    if task.terminal_boundary_enqueued_at is not None:
                        counts["enqueued"] += 1
                        continue
                    rows = await builder(db, task)
                    await db.flush()
                    if not rows or task.terminal_boundary_enqueued_at is None:
                        raise TerminalBoundaryCanonicalMismatch("reconcile builder did not enqueue a terminal boundary")
                    for row in rows:
                        if row.tenant_id != tenant_uuid or row.runtime_task_id != task.id:
                            raise TerminalBoundaryCanonicalMismatch(
                                "reconcile builder returned a cross-authority boundary"
                            )
                    counts["enqueued"] += 1
            except Exception as exc:  # noqa: BLE001 - typed ledger keeps recovery retryable.
                counts["held"] += 1
                await self._record_reconcile_failure(
                    tenant_id=tenant_uuid,
                    task_id=task_id,
                    error=exc,
                )
        return counts
