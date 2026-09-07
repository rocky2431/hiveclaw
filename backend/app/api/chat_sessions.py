"""Chat session management API endpoints."""

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timezone as tz
from pathlib import PurePosixPath
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE,
    authorize_agent_operator_inspection,
    authorize_loaded_session_access,
    check_agent_access,
    check_agent_operator_reachability,
    is_scoped_business_admin,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.audit import ChatMessage
from app.models.chat_artifact import ChatArtifact
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.chat_session import ChatSession
from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from app.runtime.ccplus_contracts import (
    DEFAULT_CCPLUS_PERMISSION_MODE,
    DEFAULT_CCPLUS_WRITABLE_ROOTS,
    normalize_permission_mode,
    tenant_permission_default_from_value as _tenant_permission_default_from_value,
)
from app.services.chat_artifact_delivery import artifact_part_from_model
from app.services.chat_message_parts import serialize_chat_message, split_inline_tools
from app.services.chat_transcript import append_session_event
from app.services.web_chat_runtime import (
    ActiveWebChatRunExists,
    broadcast_web_chat_event,
    get_active_web_chat_run,
    start_web_chat_run,
)
from app.services.web_chat_broker import web_chat_broker
from app.services.conversation_branch_service import create_conversation_branch
from app.services.session_index import read_session_index
from app.services.session_feedback import read_activation_feedback_sidecar, record_session_feedback
from app.services.decision_trace import list_session_decision_traces
from app.services.session_control_plane import build_session_json_export, build_session_workbench
from app.services.session_live_input import IdempotencyConflict, submit_live_cancel_input, submit_live_human_input
from app.services.session_tool_runtime import ToolEffectReconciliationRequired
from app.services.agent_tools import get_agent_tools_for_llm

router = APIRouter(prefix="/agents", tags=["chat-sessions"])
logger = logging.getLogger(__name__)

_LEGACY_HIDDEN_CHAT_SOURCES = ("trigger", "task", "heartbeat")
_MINE_HIDDEN_CHAT_SOURCES = _LEGACY_HIDDEN_CHAT_SOURCES
_ARTIFACT_AGENT_FIELD_PREFIXES = ("owner", "source", "download", "delivery")


async def _resolve_tenant_permission_default(db: AsyncSession, tenant_id: uuid.UUID | None) -> str:
    if tenant_id is None:
        return DEFAULT_CCPLUS_PERMISSION_MODE.value
    from app.models.tenant_setting import TenantSetting

    result = await db.execute(
        select(TenantSetting.value).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == "agent_permission_default",
        )
    )
    return _tenant_permission_default_from_value(result.scalar_one_or_none())


def _artifact_agent_ids(artifacts: list[dict]) -> set[uuid.UUID]:
    agent_ids: set[uuid.UUID] = set()
    for artifact in artifacts:
        for prefix in _ARTIFACT_AGENT_FIELD_PREFIXES:
            value = artifact.get(f"{prefix}_agent_id")
            if not value:
                continue
            try:
                agent_ids.add(uuid.UUID(str(value)))
            except (TypeError, ValueError, AttributeError):
                continue
    return agent_ids


async def _enrich_artifact_agent_names(db: AsyncSession, artifacts: list[dict]) -> None:
    agent_ids = _artifact_agent_ids(artifacts)
    if not agent_ids:
        return
    result = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))
    name_by_id = {str(agent_id): name for agent_id, name in result.all() if name}
    if not name_by_id:
        return
    for artifact in artifacts:
        for prefix in _ARTIFACT_AGENT_FIELD_PREFIXES:
            name_key = f"{prefix}_agent_name"
            if artifact.get(name_key):
                continue
            agent_id = artifact.get(f"{prefix}_agent_id")
            name = name_by_id.get(str(agent_id)) if agent_id else None
            if name:
                artifact[name_key] = name


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    user_id: str
    username: Optional[str] = None  # display_name ?? username
    source_channel: str = "web"  # web / feishu / discord / slack / agent
    session_kind: str = "human_chat"
    actor_type: str = "user"
    runtime_source: str = "web_chat"
    visibility_scope: str = "direct_user"
    listed_surface: str = "chat"
    parent_session_id: Optional[str] = None
    root_session_id: Optional[str] = None
    runtime_task_id: Optional[str] = None
    title: str
    created_at: str
    last_message_at: Optional[str] = None
    message_count: int = 0
    permission_mode: str = DEFAULT_CCPLUS_PERMISSION_MODE.value
    permission_profile: dict[str, Any] = Field(default_factory=dict)
    writable_roots: list[str] = Field(default_factory=list)
    is_current_user_session: bool = False
    read_only: bool = False
    authority_source: str | None = None
    operator_view: bool = False
    active_projection: dict[str, Any] | None = None
    # Agent-to-agent session fields
    peer_agent_id: Optional[str] = None
    peer_agent_name: Optional[str] = None
    participant_type: str = "user"  # 'user' | 'agent'


def _session_view_flags(session: ChatSession, current_user: User) -> dict[str, bool]:
    is_current_user_session = str(getattr(session, "user_id", "")) == str(getattr(current_user, "id", ""))
    source_channel = str(getattr(session, "source_channel", "") or "").lower()
    participant_type = str(getattr(session, "participant_type", "") or "").lower()
    session_kind = str(getattr(session, "session_kind", "") or "").lower()
    is_agent_session = (
        source_channel == "agent" or participant_type == "agent" or session_kind in {"agent_chat", "delegation_run"}
    )
    return {
        "is_current_user_session": is_current_user_session,
        "read_only": (not is_current_user_session) or is_agent_session,
    }


def _session_contract_fields(session: ChatSession) -> dict[str, Any]:
    session_metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
    raw_projection = session_metadata.get("active_projection")
    active_projection: dict[str, Any] | None = None
    if isinstance(raw_projection, dict):
        projection_reason = str(raw_projection.get("projection_reason") or "").strip()
        checkpoint_event_id = str(raw_projection.get("checkpoint_event_id") or "").strip()
        if projection_reason and checkpoint_event_id:
            active_projection = {
                key: raw_projection[key]
                for key in (
                    "projection_reason",
                    "checkpoint_event_id",
                    "draft_content",
                    "turn_index",
                    "applied_at",
                    "truth_source",
                    "mode",
                )
                if key in raw_projection
            }
    return {
        "session_kind": getattr(session, "session_kind", None) or "human_chat",
        "actor_type": getattr(session, "actor_type", None) or "user",
        "runtime_source": getattr(session, "runtime_source", None) or "web_chat",
        "visibility_scope": getattr(session, "visibility_scope", None) or "direct_user",
        "listed_surface": getattr(session, "listed_surface", None) or "chat",
        "parent_session_id": str(session.parent_session_id) if getattr(session, "parent_session_id", None) else None,
        "root_session_id": str(session.root_session_id) if getattr(session, "root_session_id", None) else None,
        "runtime_task_id": str(session.runtime_task_id) if getattr(session, "runtime_task_id", None) else None,
        "active_projection": active_projection,
        **_session_permission_metadata(
            str(session_metadata.get("permission_mode") or DEFAULT_CCPLUS_PERMISSION_MODE.value), session
        ),
    }


def _session_permission_metadata(
    permission_mode: str | None,
    session: ChatSession | None = None,
    *,
    allowed_tools: list[str] | None = None,
    writable_roots: list[str] | None = None,
    exact_scope: bool | None = None,
) -> dict[str, Any]:
    session_metadata = dict(getattr(session, "transcript_metadata_json", None) or {}) if session is not None else {}
    raw_profile = session_metadata.get("permission_profile")
    profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
    requested_mode = permission_mode or session_metadata.get("permission_mode") or DEFAULT_CCPLUS_PERMISSION_MODE.value
    mode = normalize_permission_mode(requested_mode).value
    if allowed_tools is None:
        allowed_tools = [
            str(item)
            for item in (profile.get("allowed_tools") or session_metadata.get("session_permission_allowed_tools") or [])
            if str(item).strip()
        ]
    session_grants = [
        dict(item)
        for item in (session_metadata.get("session_permission_grants") or profile.get("session_grants") or [])
        if isinstance(item, dict)
    ]
    if writable_roots is None:
        source_roots = profile.get("writable_roots") or session_metadata.get("writable_roots")
        writable_roots = (
            [str(item) for item in source_roots if str(item).strip()]
            if isinstance(source_roots, (list, tuple))
            else list(DEFAULT_CCPLUS_WRITABLE_ROOTS)
        )
    capability_snapshot = (
        dict(profile.get("capability_policy_snapshot"))
        if isinstance(profile.get("capability_policy_snapshot"), dict)
        else {}
    )
    if exact_scope is True:
        capability_snapshot["session_exact_scope"] = True
    elif exact_scope is False:
        capability_snapshot.pop("session_exact_scope", None)
    profile_payload: dict[str, Any] = {
        "mode": mode,
        "allowed_tools": allowed_tools,
        "writable_roots": writable_roots,
        "session_grants": session_grants,
    }
    if capability_snapshot.get("session_exact_scope") is True:
        profile_payload.update(
            {
                "readable_roots": writable_roots,
                "capability_policy_snapshot": {"session_exact_scope": True},
            }
        )
    return {
        "permission_mode": mode,
        "writable_roots": writable_roots,
        "permission_profile": profile_payload,
    }


def _has_exact_session_scope(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    profile = metadata.get("permission_profile")
    if not isinstance(profile, dict):
        return False
    snapshot = profile.get("capability_policy_snapshot")
    return isinstance(snapshot, dict) and snapshot.get("session_exact_scope") is True


def _session_out(
    session: ChatSession,
    current_user: User,
    *,
    message_count: int = 0,
    username: str | None = None,
    peer_agent_id: str | None = None,
    peer_agent_name: str | None = None,
    participant_type: str = "user",
) -> SessionOut:
    return SessionOut(
        id=str(session.id),
        agent_id=str(session.agent_id),
        user_id=str(session.user_id),
        username=username,
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
        message_count=message_count,
        **_session_view_flags(session, current_user),
        peer_agent_id=peer_agent_id,
        peer_agent_name=peer_agent_name,
        participant_type=participant_type,
        **_session_contract_fields(session),
    )


def _session_id_text(session: ChatSession) -> str:
    return str(getattr(session, "id"))


async def _load_grouped_message_counts(
    db: AsyncSession,
    session_ids: list[str],
    *,
    agent_id: uuid.UUID | None = None,
    role: str | None = None,
) -> dict[str, int]:
    if not session_ids:
        return {}
    stmt = select(ChatMessage.conversation_id, func.count(ChatMessage.id)).where(
        ChatMessage.conversation_id.in_(session_ids)
    )
    if agent_id is not None:
        stmt = stmt.where(ChatMessage.agent_id == agent_id)
    if role is not None:
        stmt = stmt.where(ChatMessage.role == role)
    stmt = stmt.group_by(ChatMessage.conversation_id)
    result = await db.execute(stmt)
    return {str(row[0]): int(row[1] or 0) for row in result.all()}


async def _load_mine_message_counts(
    db: AsyncSession,
    sessions: list[ChatSession],
    *,
    agent_id: uuid.UUID,
    role: str | None = None,
) -> dict[str, int]:
    agent_session_ids = [_session_id_text(session) for session in sessions if session.source_channel == "agent"]
    direct_session_ids = [_session_id_text(session) for session in sessions if session.source_channel != "agent"]
    filters = []
    if agent_session_ids:
        filters.append(ChatMessage.conversation_id.in_(agent_session_ids))
    if direct_session_ids:
        filters.append((ChatMessage.conversation_id.in_(direct_session_ids)) & (ChatMessage.agent_id == agent_id))
    if not filters:
        return {}
    stmt = select(ChatMessage.conversation_id, func.count(ChatMessage.id)).where(or_(*filters))
    if role is not None:
        stmt = stmt.where(ChatMessage.role == role)
    stmt = stmt.group_by(ChatMessage.conversation_id)
    result = await db.execute(stmt)
    return {str(row[0]): int(row[1] or 0) for row in result.all()}


async def _load_user_display_names(db: AsyncSession, sessions: list[ChatSession]) -> dict[str, str]:
    user_ids = {getattr(session, "user_id", None) for session in sessions if getattr(session, "user_id", None)}
    if not user_ids:
        return {}
    result = await db.execute(
        select(User.id, func.coalesce(User.display_name, User.username)).where(User.id.in_(user_ids))
    )
    return {str(row[0]): str(row[1] or "Unknown") for row in result.all()}


async def _load_agent_names(db: AsyncSession, sessions: list[ChatSession]) -> dict[str, str]:
    agent_ids = set()
    for session in sessions:
        if session.source_channel != "agent":
            continue
        if getattr(session, "agent_id", None):
            agent_ids.add(session.agent_id)
        if getattr(session, "peer_agent_id", None):
            agent_ids.add(session.peer_agent_id)
    if not agent_ids:
        return {}
    result = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))
    return {str(row[0]): str(row[1] or "Agent") for row in result.all()}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _metadata_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _metadata_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


_KNOWLEDGE_TOOL_NAMES = {
    "search_personal_kb",
    "read_personal_kb",
    "search_company_kb",
    "read_company_kb",
    "query_company_ontology",
    "get_company_object",
    "explain_company_fact",
}


def _session_context_usage_payload(session: ChatSession) -> dict[str, Any]:
    from app.runtime.context import runtime_assembly_metadata

    metadata = _metadata_dict(getattr(session, "transcript_metadata_json", None))
    assembly_state = runtime_assembly_metadata(metadata)
    prompt_manifest = _metadata_dict(assembly_state.get("prompt_assembly_manifest"))
    context_usage_ledger = _metadata_dict(
        prompt_manifest.get("context_usage_ledger") or assembly_state.get("context_usage_ledger")
    )
    dynamic_context_section_ledger = _metadata_dict(
        prompt_manifest.get("dynamic_context_section_ledger") or assembly_state.get("dynamic_context_section_ledger")
    )
    categories = _metadata_list(context_usage_ledger.get("categories"))
    context_candidates = _metadata_list(prompt_manifest.get("context_candidates"))
    selected_contexts = _metadata_list(prompt_manifest.get("selected_contexts"))
    suppressed_contexts = _metadata_list(prompt_manifest.get("suppressed_contexts"))
    dynamic_context_sections = _metadata_list(dynamic_context_section_ledger.get("sections"))
    cache_decisions = _metadata_list(assembly_state.get("cache_decision_ledger"))
    agent_cycle_decisions = _metadata_list(assembly_state.get("agent_cycle_decision_ledger"))
    activation_candidates = _metadata_list(assembly_state.get("activation_candidates"))
    tool_result_ledger = _metadata_list(assembly_state.get("tool_result_ledger"))
    knowledge_tool_results = [
        entry
        for entry in tool_result_ledger
        if str(_metadata_dict(entry).get("tool_name") or "") in _KNOWLEDGE_TOOL_NAMES
    ]
    context_artifacts = _metadata_list(metadata.get("context_artifacts") or assembly_state.get("context_artifacts"))
    active_tools = _metadata_list(prompt_manifest.get("active_tool_names") or metadata.get("active_tool_names"))
    deferred_tools = _metadata_list(
        prompt_manifest.get("available_deferred_tools")
        or metadata.get("deferred_tool_names")
        or assembly_state.get("available_deferred_tools")
    )
    loaded_skills = _metadata_list(prompt_manifest.get("loaded_skills") or metadata.get("skill_catalog_refs"))
    payload = {
        "schema": "hive.ccplus.session_context_usage.v1",
        "session_id": str(getattr(session, "id", "")),
        "agent_id": str(getattr(session, "agent_id", "")),
        "model_window_tokens": context_usage_ledger.get("model_window_tokens"),
        "used_tokens": context_usage_ledger.get("used_tokens"),
        "free_space_tokens": context_usage_ledger.get("free_space_tokens"),
        "categories": categories,
        "context_candidates": context_candidates,
        "selected_contexts": selected_contexts,
        "suppressed_contexts": suppressed_contexts,
        "dynamic_context_sections": dynamic_context_sections,
        "tool_result_ledger": tool_result_ledger,
        "knowledge_tool_results": knowledge_tool_results,
        "cache_decision_ledger": cache_decisions,
        "agent_cycle_decision_ledger": agent_cycle_decisions,
        "activation_candidates": activation_candidates,
        "context_artifacts": context_artifacts,
        "active_tool_names": active_tools,
        "deferred_tool_names": deferred_tools,
        "loaded_skills": loaded_skills,
        "prompt_manifest_schema": prompt_manifest.get("schema"),
        "runtime_assembly_state_schema": assembly_state.get("schema"),
        "counts": {
            "categories": len(categories),
            "context_candidates": len(context_candidates),
            "selected_contexts": len(selected_contexts),
            "suppressed_contexts": len(suppressed_contexts),
            "dynamic_context_sections": len(dynamic_context_sections),
            "cache_decisions": len(cache_decisions),
            "agent_cycle_decisions": len(agent_cycle_decisions),
            "activation_candidates": len(activation_candidates),
            "knowledge_tool_results": len(knowledge_tool_results),
            "context_artifacts": len(context_artifacts),
            "tools": len(active_tools),
            "deferred_tools": len(deferred_tools),
            "skills": len(loaded_skills),
        },
    }
    return _json_ready(payload)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


class CreateSessionIn(BaseModel):
    title: Optional[str] = None
    permission_mode: Literal["default", "auto", "bypassPermissions"] | None = None
    allowed_tools: list[str] | None = None
    writable_roots: list[str] | None = None


class PatchSessionIn(BaseModel):
    title: str


class UpdateSessionPermissionProfileIn(BaseModel):
    permission_mode: Literal["default", "auto", "bypassPermissions"] = DEFAULT_CCPLUS_PERMISSION_MODE.value
    allowed_tools: list[str] | None = None
    writable_roots: list[str] | None = None


class StartSessionRunIn(BaseModel):
    content: str
    display_content: str = ""
    file_name: str = ""
    plan_mode_requested: bool = False
    permission_mode: Literal["default", "auto", "bypassPermissions"] | None = None
    model_routing_locked: bool = False
    attachments: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    input_id: Optional[uuid.UUID] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=200)


class CreateSessionRunIn(StartSessionRunIn):
    title: Optional[str] = None


def _invalid_session_scope(detail: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "invalid_session_permission_scope", "message": detail},
    )


def _canonical_session_scope_root(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or "\\" in raw:
        raise _invalid_session_scope("writable_roots must contain one canonical workspace subdirectory")
    path = PurePosixPath(raw)
    parts = path.parts
    normalized = path.as_posix()
    if (
        path.is_absolute()
        or len(parts) < 2
        or parts[0] != "workspace"
        or any(part in {"", ".", ".."} for part in parts)
        or normalized != raw
    ):
        raise _invalid_session_scope("writable_roots must contain one canonical workspace subdirectory")
    return normalized


async def _validated_exact_session_scope(
    *,
    agent_id: uuid.UUID,
    allowed_tools: list[str] | None,
    writable_roots: list[str] | None,
) -> tuple[list[str], list[str]] | None:
    if allowed_tools is None and writable_roots is None:
        return None
    if not allowed_tools or not writable_roots or len(writable_roots) != 1:
        raise _invalid_session_scope("allowed_tools and exactly one writable root are required together")
    normalized_tools = [str(name).strip() for name in allowed_tools]
    if any(not name or name != original for name, original in zip(normalized_tools, allowed_tools, strict=True)) or len(
        normalized_tools
    ) != len(set(normalized_tools)):
        raise _invalid_session_scope("allowed_tools must contain unique canonical tool names")
    root = _canonical_session_scope_root(writable_roots[0])
    available = await get_agent_tools_for_llm(agent_id, core_only=False)
    available_names = {
        str(tool.get("function", {}).get("name") or "")
        for tool in available
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    unavailable = [name for name in normalized_tools if name not in available_names]
    if unavailable:
        raise _invalid_session_scope("allowed_tools contains tools that are not available to this Agent")
    return normalized_tools, [root]


class BranchSessionIn(BaseModel):
    mode: Literal[
        "fork",
        "branch",
        "edit",
        "insert_before",
        "insert_after",
        "reply",
        "regenerate",
        "side_question",
    ]
    anchor_event_id: uuid.UUID
    content: str = ""
    display_content: str = ""
    file_name: str = ""
    title: Optional[str] = None
    start_run: bool = True
    permission_mode: Literal["default", "auto", "bypassPermissions"] = DEFAULT_CCPLUS_PERMISSION_MODE.value
    model_routing_locked: bool = False
    attachments: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []


class SteerSessionTurnIn(BaseModel):
    content: str
    display_content: str = ""
    file_name: str = ""
    expected_turn_id: Optional[str] = None
    permission_mode: Literal["default", "auto", "bypassPermissions"] = DEFAULT_CCPLUS_PERMISSION_MODE.value
    attachments: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    input_id: Optional[uuid.UUID] = None
    idempotency_key: Optional[str] = None
    expected_run_id: Optional[uuid.UUID] = None
    terminal_fallback: Literal["queue_next_turn", "reject"] = "queue_next_turn"


class SessionHumanInputIn(BaseModel):
    kind: Literal[
        "start_turn",
        "steer_current_turn",
        "queue_next_turn",
        "interrupt_and_replace",
        "answer_request",
        "fork_side_thread",
    ]
    input_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    content_parts: list[dict[str, Any]]
    expected_turn_id: Optional[str] = None
    expected_run_id: Optional[uuid.UUID] = None
    terminal_fallback: Optional[Literal["queue_next_turn", "reject"]] = None
    request_item_id: Optional[uuid.UUID] = None
    fork_after_sequence: Optional[int] = Field(default=None, ge=1)
    plan_mode_requested: bool = False
    permission_mode: Literal["default", "auto", "bypassPermissions"] | None = None
    model_routing_locked: bool = False


class ReviseSessionHumanInputIn(BaseModel):
    content_parts: list[dict[str, Any]]


class ResolveSessionPermissionIn(BaseModel):
    action: Literal["allow_once", "allow_session", "deny"]
    feedback: str = ""


class SessionRunOut(BaseModel):
    run_id: str
    status: str
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result_summary: Optional[str] = None


class BranchSessionOut(BaseModel):
    session: SessionOut
    branch: dict[str, Any]
    run: Optional[dict[str, Any]] = None


class CreateSessionRunOut(BaseModel):
    session: SessionOut
    run: dict[str, Any]


class RecordSessionFeedbackIn(BaseModel):
    label: Literal["useful", "misleading"]
    reason: str = ""
    message_id: Optional[uuid.UUID] = None
    decision_id: Optional[str] = None


class SessionDecisionTraceOut(BaseModel):
    id: str
    action: str
    tool_name: Optional[str] = None
    outcome: str
    reason_codes: list[str] = Field(default_factory=list)
    created_at: str
    feedback_count: int = Field(ge=0)


def _transcript_role_for_event(event: ChatTranscriptEvent) -> str:
    metadata = event.metadata_json or {}
    role = metadata.get("role")
    if isinstance(role, str) and role:
        return role
    if event.event_type == "user_message":
        return "user"
    if event.event_type in {"assistant_message", "response_repair"}:
        return "assistant"
    if event.event_type in {"tool_result", "tool_call"}:
        return "tool_call"
    return "event"


_TRANSCRIPT_CONTENT_CHAR_LIMIT = 4_000
_TRANSCRIPT_PART_TEXT_CHAR_LIMIT = 512
_TRANSCRIPT_METADATA_TEXT_CHAR_LIMIT = 512
_TRANSCRIPT_METADATA_VALUE_BYTE_LIMIT = 2_000
_TRANSCRIPT_PART_VALUE_BYTE_LIMIT = 2_000
_TRANSCRIPT_HEAVY_PAYLOAD_KEYS = {
    "blob",
    "bytes",
    "content_replacement",
    "data",
    "file_content",
    "html",
    "inline_content",
    "payload",
    "raw",
    "text_delta",
}
_TRANSCRIPT_METADATA_KEEP_KEYS = {
    "artifact_id",
    "artifact_ids",
    "command",
    "filename",
    "kind",
    "message_id",
    "original_message_id",
    "original_transcript_event_id",
    "repair_version",
    "supersedes_message_id",
    "mime_type",
    "name",
    "path",
    "role",
    "run_id",
    "size",
    "source",
    "status",
    "summary",
    "title",
    "tool_call_id",
    "tool_name",
    "turn_id",
    "type",
    "url",
}
_TRANSCRIPT_INTERACTIVE_TOOL_RESULT_STATUSES = {
    "awaiting_user_clarification",
    "dynamic_workflow_proposed",
    "needs_plan",
    "plan_mode_entry_requested",
    "planning_failed",
}
_TRANSCRIPT_INTERACTIVE_TOOL_PAYLOAD_KEYS = (
    "name",
    "status",
    "tool_call_id",
    "step_id",
    "duration_ms",
    "visibility",
)


def _json_size_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return _TRANSCRIPT_METADATA_VALUE_BYTE_LIMIT + 1


def _truncate_text_for_transcript(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return f"{value[:limit]}...[truncated]", True


def _inline_content_replacement(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    inline_content = value.get("inline_content")
    return inline_content if isinstance(inline_content, str) and inline_content.strip() else None


def _interactive_tool_result_status_allowed(tool_name: str, status: Any) -> bool:
    if status in _TRANSCRIPT_INTERACTIVE_TOOL_RESULT_STATUSES:
        return True
    if status == "preview" and tool_name == "preview_agent_blueprint":
        return True
    return status == "success" and tool_name == "create_digital_employee"


def _project_interactive_tool_card_content(event: ChatTranscriptEvent, content: str) -> str | None:
    if getattr(event, "event_type", None) not in {"tool_call", "tool_result"}:
        return None

    payload = _json_object(content)
    if not payload:
        return None

    result_value = payload.get("result")
    result_payload = _json_object(result_value)
    if not result_payload:
        inline_content = _inline_content_replacement(payload.get("content_replacement"))
        if inline_content:
            result_value = inline_content
            result_payload = _json_object(inline_content)

    metadata = getattr(event, "metadata_json", None) or {}
    metadata_tool_name = metadata.get("tool_name") if isinstance(metadata, dict) else None
    payload_tool_name = payload.get("name")
    tool_name = (
        payload_tool_name
        if isinstance(payload_tool_name, str) and payload_tool_name
        else metadata_tool_name
        if isinstance(metadata_tool_name, str) and metadata_tool_name
        else ""
    )
    if not _interactive_tool_result_status_allowed(tool_name, result_payload.get("status")):
        return None

    projected: dict[str, Any] = {}
    for key in _TRANSCRIPT_INTERACTIVE_TOOL_PAYLOAD_KEYS:
        if key in payload:
            projected[key] = payload[key]
    if "name" not in projected:
        if tool_name:
            projected["name"] = tool_name
    projected["result"] = (
        result_value
        if isinstance(result_value, str) and result_value.strip()
        else json.dumps(result_payload, ensure_ascii=False, default=str)
    )
    return json.dumps(projected, ensure_ascii=False, default=str)


def _compact_transcript_json_value(
    value: Any,
    *,
    text_limit: int,
    byte_limit: int,
) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _truncate_text_for_transcript(value, text_limit)
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if _json_size_bytes(value) <= byte_limit:
        return value, False
    if isinstance(value, list):
        compact_items: list[Any] = []
        truncated = False
        for item in value[:20]:
            compact_item, item_truncated = _compact_transcript_json_value(
                item,
                text_limit=min(text_limit, 512),
                byte_limit=min(byte_limit, 2_000),
            )
            compact_items.append(compact_item)
            truncated = truncated or item_truncated
        if len(value) > 20:
            truncated = True
        return compact_items, True
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        truncated = False
        for key, item in value.items():
            if key in _TRANSCRIPT_HEAVY_PAYLOAD_KEYS:
                truncated = True
                continue
            if _json_size_bytes(item) > byte_limit:
                truncated = True
                continue
            compact_item, item_truncated = _compact_transcript_json_value(
                item,
                text_limit=min(text_limit, 512),
                byte_limit=min(byte_limit, 2_000),
            )
            compact[str(key)] = compact_item
            truncated = truncated or item_truncated
        return compact, True
    return {"omitted_oversize_value": True, "transcript_projection": True}, True


def _compact_transcript_part(part: Any) -> tuple[Any, bool]:
    if not isinstance(part, dict):
        return _compact_transcript_json_value(
            part,
            text_limit=_TRANSCRIPT_PART_TEXT_CHAR_LIMIT,
            byte_limit=_TRANSCRIPT_PART_VALUE_BYTE_LIMIT,
        )

    compact: dict[str, Any] = {}
    omitted_keys: list[str] = []
    truncated = False
    for key, value in part.items():
        key = str(key)
        if key in _TRANSCRIPT_HEAVY_PAYLOAD_KEYS and _json_size_bytes(value) > 512:
            omitted_keys.append(key)
            truncated = True
            continue
        if _json_size_bytes(value) > _TRANSCRIPT_PART_VALUE_BYTE_LIMIT:
            omitted_keys.append(key)
            truncated = True
            continue
        compact_value, value_truncated = _compact_transcript_json_value(
            value,
            text_limit=_TRANSCRIPT_PART_TEXT_CHAR_LIMIT,
            byte_limit=_TRANSCRIPT_PART_VALUE_BYTE_LIMIT,
        )
        compact[key] = compact_value
        truncated = truncated or value_truncated
    if omitted_keys:
        compact["_omitted_keys"] = omitted_keys
    if truncated:
        compact["_payload_truncated"] = True
    return compact, truncated


def _compact_transcript_metadata(metadata: Any, truncations: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}

    compact: dict[str, Any] = {}
    for key, value in metadata.items():
        key = str(key)
        value_size = _json_size_bytes(value)
        if key in _TRANSCRIPT_HEAVY_PAYLOAD_KEYS and value_size > 512:
            truncations.append({"field": f"metadata.{key}", "original_bytes": value_size})
            continue
        if key not in _TRANSCRIPT_METADATA_KEEP_KEYS and value_size > _TRANSCRIPT_METADATA_VALUE_BYTE_LIMIT:
            truncations.append({"field": f"metadata.{key}", "original_bytes": value_size})
            continue
        compact_value, truncated = _compact_transcript_json_value(
            value,
            text_limit=_TRANSCRIPT_METADATA_TEXT_CHAR_LIMIT,
            byte_limit=_TRANSCRIPT_METADATA_VALUE_BYTE_LIMIT,
        )
        compact[key] = compact_value
        if truncated:
            truncations.append({"field": f"metadata.{key}", "original_bytes": value_size})
    return compact


def _serialize_transcript_event(event: ChatTranscriptEvent, *, audience: str = "operator") -> dict:
    from app.services.thread_items import build_thread_item

    truncations: list[dict[str, Any]] = []
    content = event.content or ""
    projected_content = _project_interactive_tool_card_content(event, content)
    event_type = str(getattr(event, "event_type", "") or "")
    if event_type in {"user_message", "assistant_message", "response_repair", "summary_turn"}:
        # These bytes are the user/model-authored product output or the exact
        # recovery summary. UI payload economics must never replace their tail
        # with a platform-authored truncation marker.
        pass
    elif projected_content is not None:
        content = projected_content
        truncations.append(
            {
                "field": "content",
                "projection": "interactive_tool_card",
                "original_chars": len(event.content or ""),
            }
        )
    else:
        content, content_truncated = _truncate_text_for_transcript(content, _TRANSCRIPT_CONTENT_CHAR_LIMIT)
        if content_truncated:
            truncations.append({"field": "content", "original_chars": len(event.content or "")})

    parts: list[Any] = []
    for index, part in enumerate(event.parts_json or []):
        compact_part, truncated = _compact_transcript_part(part)
        parts.append(compact_part)
        if truncated:
            truncations.append({"field": f"parts[{index}]", "original_bytes": _json_size_bytes(part)})

    metadata = _compact_transcript_metadata(event.metadata_json or {}, truncations)
    if truncations:
        metadata["_payload_truncated"] = True
        metadata["_payload_truncations"] = truncations

    return build_thread_item(
        event,
        content=content,
        parts=parts,
        metadata=metadata,
        role=_transcript_role_for_event(event),
        audience="operator" if audience == "operator" else "user",
        preserve_user_content=(
            projected_content is not None
            or event_type in {"user_message", "assistant_message", "response_repair", "summary_turn"}
        ),
    )


async def _get_run_session_and_agent(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User,
    action: str = "use_session",
    operator_view: bool = False,
    operator_reason: str | None = None,
    require_writable: bool = False,
) -> tuple[ChatSession, Agent, str]:
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if operator_view and not require_writable
        else check_agent_access(db, current_user, agent_id)
    )
    authority_source = await _authorize_loaded_session(
        db=db,
        session=session,
        agent=agent,
        access_level=access_level,
        current_user=current_user,
        action=action,
        operator_view=operator_view,
        operator_reason=operator_reason,
        require_writable=require_writable,
    )
    return session, agent, authority_source


async def _audit_scoped_admin_session_collection(
    db: AsyncSession,
    *,
    current_user: User,
    agent: Agent,
    sessions: list[ChatSession],
) -> None:
    """Audit one administrator business listing of a managed session inventory.

    One row per request naming the deduplicated real user/owner target set of
    the sessions actually exposed by ``scope=all`` — schema-consistent with
    the per-session writer and the other collection writers, including the
    explicit ``outcome``. Written through the request transaction before the
    payload returns: an audit failure denies the listing instead of silently
    releasing the business inventory.
    """

    from app.core.policy import write_audit_event

    target_user_ids = sorted(
        {str(session.user_id) for session in sessions if getattr(session, "user_id", None) is not None}
    )
    await write_audit_event(
        db,
        event_type="session.scoped_business_admin_access",
        severity="info",
        actor_type="user",
        actor_id=current_user.id,
        tenant_id=agent.tenant_id,
        action="chat_session_collection:read",
        resource_type="chat_session_collection",
        resource_id=uuid.uuid5(agent.id, "chat-session-collection"),
        details={
            "agent_id": str(agent.id),
            "actor_role": str(getattr(current_user, "role", "") or ""),
            "authority_source": SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE,
            "outcome": "allowed",
            "session_user_id": target_user_ids[0] if len(target_user_ids) == 1 else None,
            "target_user_ids": target_user_ids[:50],
            "target_count": len(target_user_ids),
        },
    )


async def _authorize_loaded_session(
    *,
    db: AsyncSession,
    session: ChatSession,
    agent: Agent,
    access_level: str,
    current_user: User,
    action: str,
    operator_view: bool = False,
    operator_reason: str | None = None,
    require_writable: bool = False,
) -> str:
    # Single shared decision (PDEC-013): session owner, scoped business
    # administrator acting as themselves, or the audited operator inspection
    # lane. Delegating here keeps the HTTP layer and the durable command
    # resolver in exact agreement.
    return await authorize_loaded_session_access(
        db,
        current_user,
        agent=agent,
        session=session,
        access_level=access_level,
        action=action,
        operator_view=operator_view,
        operator_reason=operator_reason,
        require_writable=require_writable,
    )


@router.get("/{agent_id}/sessions")
async def list_sessions(
    agent_id: uuid.UUID,
    scope: str = Query("mine", description="'mine' or 'all'"),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List chat sessions for an agent. 'all' shows every session to a scoped
    business administrator (PDEC-013) and otherwise requires the audited
    operator inspection lane."""
    agent, _access_level = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if scope == "all"
        else check_agent_access(db, current_user, agent_id)
    )

    if scope == "all":
        if is_scoped_business_admin(current_user, resource_tenant_id=getattr(agent, "tenant_id", None)):
            # Administrators list the managed business inventory as themselves:
            # no manual operator reason, no operator.inspect grant, one
            # audited collection decision recording the real actor and scope.
            # The event is written once the actually-exposed target set is
            # known, before the payload returns.
            authority_source = SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE
        else:
            authority_source = await authorize_agent_operator_inspection(
                db,
                user=current_user,
                agent=agent,
                reason=operator_reason,
                action="chat_session_collection:read",
                resource_type="chat_session_collection",
                resource_id=uuid.uuid5(agent_id, "chat-session-collection"),
            )

        # Fetch all sessions (including agent-to-agent where this agent is peer)
        result = await db.execute(
            select(ChatSession)
            .where(
                (ChatSession.agent_id == agent_id)
                | ((ChatSession.peer_agent_id == agent_id) & (ChatSession.source_channel == "agent")),
                ChatSession.listed_surface == "chat",
                ChatSession.source_channel.notin_(_LEGACY_HIDDEN_CHAT_SOURCES),
            )
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
        )
        sessions = result.scalars().all()
        message_counts = await _load_grouped_message_counts(db, [_session_id_text(session) for session in sessions])
        user_names = await _load_user_display_names(db, sessions)
        agent_names = await _load_agent_names(db, sessions)
        out = []
        exposed_sessions: list[ChatSession] = []
        for session in sessions:
            count = message_counts.get(_session_id_text(session), 0)
            if count == 0:
                continue  # hide empty sessions
            exposed_sessions.append(session)

            # Determine display name based on session type
            display = None
            peer_agent_id = None
            peer_agent_name = None
            participant_type = "user"

            if session.source_channel == "agent" and session.peer_agent_id:
                # Agent-to-agent session
                participant_type = "agent"
                peer_agent_id = str(session.peer_agent_id)
                a1_name = agent_names.get(str(session.agent_id), "Agent")
                a2_name = agent_names.get(str(session.peer_agent_id), "Agent")
                peer_agent_name = a2_name
                display = f"🤖 {a1_name} ↔ {a2_name}"
            else:
                display = user_names.get(str(session.user_id), "Unknown")

            out.append(
                SessionOut(
                    id=str(session.id),
                    agent_id=str(session.agent_id),
                    user_id=str(session.user_id),
                    username=display,
                    source_channel=session.source_channel,
                    title=session.title,
                    created_at=session.created_at.isoformat(),
                    last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                    message_count=count,
                    **_session_view_flags(session, current_user),
                    peer_agent_id=peer_agent_id,
                    peer_agent_name=peer_agent_name,
                    participant_type=participant_type,
                    authority_source=authority_source,
                    # Truthful projection: a scoped business administrator
                    # listing (PDEC-013) is a normal business view, not the
                    # audited operator inspection projection.
                    operator_view=authority_source != SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE,
                    **_session_contract_fields(session),
                )
            )
        if authority_source == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE:
            await _audit_scoped_admin_session_collection(
                db, current_user=current_user, agent=agent, sessions=exposed_sessions
            )
        return out

    else:  # scope == "mine"
        ownership_filter = ChatSession.user_id == current_user.id
        direct_session_filter = (ChatSession.agent_id == agent_id) & ownership_filter
        a2a_peer_session_filter = (
            (ChatSession.peer_agent_id == agent_id)
            & (ChatSession.source_channel == "agent")
            & (ChatSession.user_id == current_user.id)
        )

        result = await db.execute(
            select(ChatSession)
            .where(
                direct_session_filter | a2a_peer_session_filter,
                ChatSession.listed_surface == "chat",
                ChatSession.source_channel.notin_(_MINE_HIDDEN_CHAT_SOURCES),
            )
            .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
        )
        sessions = result.scalars().all()
        user_message_counts = await _load_mine_message_counts(db, sessions, agent_id=agent_id, role="user")
        message_counts = await _load_mine_message_counts(db, sessions, agent_id=agent_id)
        out = []
        for session in sessions:
            # Count only — skip sessions with no user messages (orphan assistant-only records)
            session_id_text = _session_id_text(session)
            user_msg_count = user_message_counts.get(session_id_text, 0)
            is_owned_direct_web_session = (
                str(session.user_id) == str(current_user.id)
                and session.source_channel == "web"
                and (getattr(session, "session_kind", None) in (None, "human_chat"))
            )
            if user_msg_count == 0 and not is_owned_direct_web_session:
                continue  # hide empty channel/A2A/orphan sessions, but keep newly created user web sessions writable.
            # Total message count for display
            count = message_counts.get(session_id_text, 0)
            peer_agent_id = None
            peer_agent_name = None
            participant_type = "user"
            username = None
            if session.source_channel == "agent" and session.peer_agent_id:
                participant_type = "agent"
                peer_agent_id = str(session.peer_agent_id)
                peer_agent_name = session.title
                username = session.title
            out.append(
                SessionOut(
                    id=str(session.id),
                    agent_id=str(session.agent_id),
                    user_id=str(session.user_id),
                    username=username,
                    source_channel=session.source_channel,
                    title=session.title,
                    created_at=session.created_at.isoformat(),
                    last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                    message_count=count,
                    **_session_view_flags(session, current_user),
                    peer_agent_id=peer_agent_id,
                    peer_agent_name=peer_agent_name,
                    participant_type=participant_type,
                    **_session_contract_fields(session),
                )
            )
        return out


@router.post("/{agent_id}/sessions", status_code=201)
async def create_session(
    agent_id: uuid.UUID,
    body: CreateSessionIn = CreateSessionIn(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new chat session for the current user."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)
    exact_scope = await _validated_exact_session_scope(
        agent_id=agent_id,
        allowed_tools=body.allowed_tools,
        writable_roots=body.writable_roots,
    )

    now = datetime.now(tz.utc)
    new_id = uuid.uuid4()
    session = ChatSession(
        id=new_id,
        agent_id=agent_id,
        tenant_id=getattr(agent, "tenant_id", getattr(current_user, "tenant_id", None)),
        user_id=current_user.id,
        title=body.title or f"Session {now.strftime('%m-%d %H:%M')}",
        created_at=now,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
    )
    permission_mode = body.permission_mode or str(
        getattr(agent, "default_session_permission_mode", "") or DEFAULT_CCPLUS_PERMISSION_MODE.value
    )
    session.transcript_metadata_json = _session_permission_metadata(
        permission_mode,
        session,
        allowed_tools=exact_scope[0] if exact_scope is not None else None,
        writable_roots=exact_scope[1] if exact_scope is not None else None,
        exact_scope=exact_scope is not None,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return _session_out(session, current_user, message_count=0)


@router.patch("/{agent_id}/sessions/{session_id}")
async def rename_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: PatchSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rename a session. Only owner, admin, or creator can rename."""
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    await _authorize_loaded_session(
        db=db,
        session=session,
        agent=agent,
        access_level=access_level,
        current_user=current_user,
        action="rename_session",
        require_writable=True,
    )

    session.title = body.title
    await db.commit()
    return {"id": str(session.id), "title": session.title}


@router.patch("/{agent_id}/sessions/{session_id}/permissions/profile")
async def update_session_permission_profile(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: UpdateSessionPermissionProfileIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the current CCPlus session permission mode immediately."""
    session, agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="update_session_permission_profile",
        require_writable=True,
    )
    exact_scope = await _validated_exact_session_scope(
        agent_id=agent.id,
        allowed_tools=body.allowed_tools,
        writable_roots=body.writable_roots,
    )
    permission_metadata = _session_permission_metadata(
        body.permission_mode,
        session,
        allowed_tools=exact_scope[0] if exact_scope is not None else None,
        writable_roots=exact_scope[1] if exact_scope is not None else None,
        exact_scope=True if exact_scope is not None else None,
    )
    active_result = await db.execute(
        select(RuntimeTask)
        .where(
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
            RuntimeTask.status.in_(("pending", "running", "suspended", "resumable")),
        )
        .order_by(RuntimeTask.created_at.desc())
        .limit(1)
    )
    active_run = active_result.scalar_one_or_none()
    if active_run is not None and (
        _has_exact_session_scope(session.transcript_metadata_json)
        or _has_exact_session_scope(getattr(active_run, "metadata_json", None))
        or exact_scope is not None
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "exact_session_permission_profile_locked",
                "message": "An exact-scoped active run keeps its admission permission profile until terminal settlement.",
            },
        )

    session_metadata = dict(session.transcript_metadata_json or {})
    session_metadata.pop("break_glass", None)
    session_metadata.update(permission_metadata)
    session.transcript_metadata_json = session_metadata
    if active_run is not None:
        active_metadata = dict(getattr(active_run, "metadata_json", None) or {})
        active_metadata.pop("break_glass", None)
        active_metadata.update(permission_metadata)
        active_run.metadata_json = active_metadata

    await append_session_event(
        db=db,
        agent_id=agent_id,
        tenant_id=session.tenant_id,
        session_id=session_id,
        actor_type="user",
        event_type="permission_profile_updated",
        content=f"Session permission mode changed to {permission_metadata['permission_mode']}",
        user_id=current_user.id,
        runtime_task_id=getattr(active_run, "id", None),
        metadata={
            **permission_metadata,
            "operator_role": getattr(current_user, "role", None),
        },
        materialize_chat_message=False,
        source="permission_profile_api",
    )

    await db.commit()

    runtime_session_context = await web_chat_broker.get_or_create_runtime_session(str(agent_id), str(session_id))
    runtime_session_context.metadata.pop("break_glass", None)
    runtime_session_context.metadata.update(permission_metadata)
    await broadcast_web_chat_event(
        agent_id,
        session_id,
        {
            "type": "permission_profile_updated",
            "event_type": "permission_profile_updated",
            **permission_metadata,
        },
    )
    return permission_metadata


@router.post("/{agent_id}/sessions/{session_id}/feedback")
async def record_feedback_for_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: RecordSessionFeedbackIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record owner/user Useful or Misleading feedback for one session."""
    session, agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    try:
        result = await record_session_feedback(
            db=db,
            agent=agent,
            session=session,
            current_user=current_user,
            label=body.label,
            reason=body.reason,
            message_id=body.message_id,
            decision_id=body.decision_id,
        )
    except (KeyError, ValueError) as exc:
        if body.decision_id is None:
            raise
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Decision not found in this session",
        ) from exc
    await db.commit()
    return result


@router.get(
    "/{agent_id}/sessions/{session_id}/decisions",
    response_model=list[SessionDecisionTraceOut],
)
async def list_decisions_for_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List governed action decisions for the authorized session."""

    session, agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="read_decision_history",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    return await list_session_decision_traces(
        db=db,
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        session_id=session.id,
        limit=limit,
    )


@router.get("/{agent_id}/sessions/{session_id}/feedback/activation-sidecar")
async def get_session_activation_feedback_sidecar(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read the activation-feedback audit sidecar for session debugging."""
    session, agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="read_activation_feedback",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    return read_activation_feedback_sidecar(
        agent_id=agent.id,
        session_id=session.id,
        limit=limit,
        newest_first=True,
    )


@router.post("/{agent_id}/sessions/runs", status_code=201, response_model=CreateSessionRunOut)
async def create_session_run(
    agent_id: uuid.UUID,
    body: CreateSessionRunIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new human web session and start its first durable run atomically."""
    agent, _access_level = await check_agent_access(db, current_user, agent_id)

    now = datetime.now(tz.utc)
    session = ChatSession(
        id=uuid.uuid4(),
        agent_id=agent_id,
        tenant_id=getattr(agent, "tenant_id", getattr(current_user, "tenant_id", None)),
        user_id=current_user.id,
        title=body.title or f"Session {now.strftime('%m-%d %H:%M')}",
        created_at=now,
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
    )
    db.add(session)
    await db.flush()
    permission_mode = (
        body.permission_mode
        or str(getattr(agent, "default_session_permission_mode", "") or "").strip()
        or await _resolve_tenant_permission_default(db, session.tenant_id)
    )
    try:
        receipt = await submit_live_human_input(
            db=db,
            agent=agent,
            user=current_user,
            session=session,
            content=body.content,
            source="legacy_rest_create_session_run",
            input_id=body.input_id,
            idempotency_key=body.idempotency_key,
            display_content=body.display_content,
            file_name=body.file_name,
            plan_mode_requested=body.plan_mode_requested,
            runtime_metadata={
                **_session_permission_metadata(permission_mode, session),
                "model_routing_locked": body.model_routing_locked,
            },
            attachments=body.attachments,
            parts=body.parts,
        )
        run = dict(receipt.get("run") or {})
    except IdempotencyConflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "idempotency_conflict", "command_id": str(exc.command_id), "receipt_ref": exc.receipt_ref},
        ) from exc
    except ToolEffectReconciliationRequired as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=exc.http_detail()) from exc

    await db.refresh(session)
    return CreateSessionRunOut(session=_session_out(session, current_user, message_count=1), run=run)


@router.post("/{agent_id}/sessions/{session_id}/runs", status_code=201)
async def start_session_run(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: StartSessionRunIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start a durable in-process web chat run for a session."""
    session, agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="start_session_run",
        require_writable=True,
    )
    try:
        receipt = await submit_live_human_input(
            db=db,
            agent=agent,
            user=current_user,
            session=session,
            content=body.content,
            source="legacy_rest_start_session_run",
            input_id=body.input_id,
            idempotency_key=body.idempotency_key,
            display_content=body.display_content,
            file_name=body.file_name,
            plan_mode_requested=body.plan_mode_requested,
            runtime_metadata={
                **_session_permission_metadata(body.permission_mode, session),
                "model_routing_locked": body.model_routing_locked,
            },
            attachments=body.attachments,
            parts=body.parts,
        )
        if receipt.get("run"):
            return receipt["run"]
        return JSONResponse(status_code=202, content=receipt)
    except IdempotencyConflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "idempotency_conflict", "command_id": str(exc.command_id), "receipt_ref": exc.receipt_ref},
        ) from exc
    except ToolEffectReconciliationRequired as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=exc.http_detail()) from exc


@router.post("/{agent_id}/sessions/{session_id}/branches", status_code=201, response_model=BranchSessionOut)
async def branch_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: BranchSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a non-destructive conversation branch from a transcript event."""
    source_session, agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="branch_session",
        require_writable=True,
    )
    branch_result = await create_conversation_branch(
        db=db,
        agent=agent,
        user=current_user,
        source_session=source_session,
        mode=body.mode,
        anchor_event_id=body.anchor_event_id,
        content=body.content,
        display_content=body.display_content,
        file_name=body.file_name,
        title=body.title,
        attachments=body.attachments,
        parts=body.parts,
    )
    run_payload: dict[str, Any] | None = None
    if body.start_run and branch_result.run_request is not None:
        request = branch_result.run_request
        try:
            runtime_metadata = {
                **(getattr(request, "extra_metadata", None) or {}),
                **_session_permission_metadata(body.permission_mode, branch_result.session),
                "model_routing_locked": body.model_routing_locked,
            }
            if request.append_user_message:
                # A content-bearing branch starts a new logical user turn. It
                # must enter through the same durable Session V2 admission and
                # round-binding lane as POST /runs; a bare RuntimeTask prompt
                # is not a provider message and would leave bound_input_ids
                # empty. Derive the IDs from the already-created branch so
                # dispatch recovery cannot duplicate its input or run.
                branch_input_id = uuid.uuid5(
                    branch_result.session.id,
                    f"conversation-branch:{body.mode}:initial-input",
                )
                receipt = await submit_live_human_input(
                    db=db,
                    agent=agent,
                    user=current_user,
                    session=branch_result.session,
                    content=request.content,
                    source=f"conversation_branch_{body.mode}",
                    input_id=branch_input_id,
                    idempotency_key=(f"conversation-branch:{branch_result.session.id}:{body.mode}:initial-input"),
                    requested_kind="start_turn",
                    display_content=request.display_content,
                    file_name=request.file_name,
                    attachments=getattr(request, "attachments", None) or [],
                    parts=getattr(request, "parts", None) or [],
                    runtime_metadata=runtime_metadata,
                )
                run_payload = dict(receipt.get("run") or {}) or None
                if run_payload is None:
                    branch_result.branch["input_receipt"] = {
                        key: receipt.get(key)
                        for key in (
                            "schema",
                            "schema_version",
                            "input_id",
                            "admission_state",
                            "reason_code",
                            "dispatch_status",
                        )
                    }
            else:
                # Regenerate reuses the copied canonical user prefix and does
                # not create a second HumanInput checkpoint.
                run_payload = await start_web_chat_run(
                    db=db,
                    agent=agent,
                    user=current_user,
                    session=branch_result.session,
                    content=request.content,
                    display_content=request.display_content,
                    file_name=request.file_name,
                    attachments=getattr(request, "attachments", None) or [],
                    parts=getattr(request, "parts", None) or [],
                    append_user_message=False,
                    extra_metadata=runtime_metadata,
                )
        except ActiveWebChatRunExists as exc:
            run_payload = {"status": "queued", **exc.run}
        except IdempotencyConflict as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "idempotency_conflict",
                    "command_id": str(exc.command_id),
                    "receipt_ref": exc.receipt_ref,
                },
            ) from exc
    else:
        await db.commit()

    session = branch_result.session
    session_out = SessionOut(
        id=str(session.id),
        agent_id=str(session.agent_id),
        user_id=str(session.user_id),
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
        message_count=0,
        **_session_view_flags(session, current_user),
        **_session_contract_fields(session),
    )
    return BranchSessionOut(session=session_out, branch=branch_result.branch, run=run_payload)


@router.get("/{agent_id}/sessions/{session_id}/branches")
async def list_session_branches(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List direct branches created from a session."""
    await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="read_branches",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.parent_session_id == session_id,
            ChatSession.listed_surface == "chat",
        )
        .order_by(ChatSession.created_at.desc())
    )
    sessions = result.scalars().all()
    return [
        SessionOut(
            id=str(session.id),
            agent_id=str(session.agent_id),
            user_id=str(session.user_id),
            source_channel=session.source_channel,
            title=session.title,
            created_at=session.created_at.isoformat(),
            last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
            message_count=0,
            **_session_view_flags(session, current_user),
            **_session_contract_fields(session),
        )
        for session in sessions
    ]


@router.get("/{agent_id}/sessions/{session_id}/lineage")
async def get_session_lineage(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the branch family for the selected session."""
    session, _agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="read_lineage",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    root_id = session.root_session_id or session.id
    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.listed_surface == "chat",
            (ChatSession.id == root_id) | (ChatSession.root_session_id == root_id),
        )
        .order_by(ChatSession.created_at.asc())
    )
    sessions = result.scalars().all()
    return [
        {
            "id": str(item.id),
            "parent_session_id": str(item.parent_session_id) if item.parent_session_id else None,
            "root_session_id": str(item.root_session_id) if item.root_session_id else None,
            "title": item.title,
            "branch": item.transcript_metadata_json or {},
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in sessions
    ]


@router.get("/{agent_id}/sessions/{session_id}/index")
async def get_session_index(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="read_session_index",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    index = await read_session_index(db, agent_id=agent_id, session_id=session_id)
    if index is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return index


@router.get("/{agent_id}/sessions/{session_id}/context-usage")
async def get_session_context_usage(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session, _agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="read_context_usage",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    return _session_context_usage_payload(session)


@router.get("/{agent_id}/sessions/{session_id}/workbench")
async def get_session_workbench(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    timeline_limit: int = 50,
    include: str = "",
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session, agent, authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="read_workbench",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    include_sections = {part.strip() for part in include.split(",") if part.strip()}
    audience = "operator" if authority_source == "operator_inspect_grant" else "user"
    payload = await build_session_workbench(
        db,
        agent=agent,
        session=session,
        timeline_limit=timeline_limit,
        include=include_sections,
        audience=audience,
    )
    await db.commit()
    return payload


@router.get("/{agent_id}/sessions/{session_id}/export")
async def export_session_json(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session, agent, authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="export_session",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    payload = await build_session_json_export(
        db,
        agent=agent,
        session=session,
        audience="operator" if authority_source == "operator_inspect_grant" else "user",
    )
    await db.commit()
    return payload


@router.get("/{agent_id}/sessions/{session_id}/runs/active")
async def get_active_session_run(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the active durable web chat run for a session, if one exists."""
    await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="read_active_run",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    return await get_active_web_chat_run(db=db, agent_id=agent_id, session_id=session_id)


def _session_input_runtime_content(content_parts: list[dict[str, Any]]) -> str:
    if len(content_parts) == 1 and isinstance(content_parts[0], dict):
        for key in ("text", "content"):
            value = content_parts[0].get(key)
            if isinstance(value, str):
                return value
    return json.dumps(content_parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _if_match_revision(value: str | None) -> int:
    clean = str(value or "").strip()
    if clean.startswith("W/"):
        clean = clean[2:].strip()
    clean = clean.strip('"')
    try:
        revision = int(clean)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=428, detail="If-Match input revision is required") from exc
    if revision <= 0:
        raise HTTPException(status_code=428, detail="If-Match input revision must be positive")
    return revision


def _validate_session_input_shape(body: SessionHumanInputIn) -> None:
    if not body.content_parts:
        raise HTTPException(status_code=422, detail="content_parts must not be empty")
    if body.kind in {"steer_current_turn", "interrupt_and_replace"}:
        if body.expected_turn_id is None or body.expected_run_id is None:
            raise HTTPException(status_code=422, detail=f"{body.kind} requires expected_turn_id and expected_run_id")
    if body.kind == "steer_current_turn" and body.terminal_fallback is None:
        raise HTTPException(status_code=422, detail="steer_current_turn requires terminal_fallback")
    if body.kind == "answer_request" and body.request_item_id is None:
        raise HTTPException(status_code=422, detail="answer_request requires request_item_id")
    if body.kind == "fork_side_thread" and body.fork_after_sequence is None:
        raise HTTPException(status_code=422, detail="fork_side_thread requires fork_after_sequence")


async def _submit_session_human_input(
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: SessionHumanInputIn,
    current_user: User,
    db: AsyncSession,
) -> dict[str, Any]:
    _validate_session_input_shape(body)
    session, agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="submit_session_human_input",
        require_writable=True,
    )
    try:
        receipt = await submit_live_human_input(
            db=db,
            agent=agent,
            user=current_user,
            session=session,
            content="",
            parts=body.content_parts,
            source="session_input_api",
            input_id=body.input_id,
            idempotency_key=body.idempotency_key,
            requested_kind=body.kind,
            expected_turn_id=body.expected_turn_id,
            expected_run_id=body.expected_run_id,
            terminal_fallback=body.terminal_fallback or "queue_next_turn",
            request_item_id=body.request_item_id,
            fork_after_sequence=body.fork_after_sequence,
            plan_mode_requested=body.plan_mode_requested,
            runtime_metadata={
                **_session_permission_metadata(body.permission_mode, session),
                "model_routing_locked": body.model_routing_locked,
            },
        )
    except IdempotencyConflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_conflict",
                "command_id": str(exc.command_id),
                "receipt_ref": exc.receipt_ref,
            },
        ) from exc
    except ToolEffectReconciliationRequired as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=exc.http_detail()) from exc
    dispatch = dict(receipt.get("dispatch") or {})
    return {
        **receipt,
        "replacement": dispatch if dispatch.get("kind") == "replacement" else None,
        "fork": dispatch if dispatch.get("kind") == "fork" else None,
    }


@router.post("/{agent_id}/sessions/{session_id}/inputs")
async def submit_session_human_input(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: SessionHumanInputIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Accept, Hook-admit and dispatch one explicit HumanInput intent."""

    return await _submit_session_human_input(
        agent_id=agent_id,
        session_id=session_id,
        body=body,
        current_user=current_user,
        db=db,
    )


@router.patch("/{agent_id}/sessions/{session_id}/inputs/{input_id}")
async def revise_session_human_input(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    input_id: uuid.UUID,
    body: ReviseSessionHumanInputIn,
    if_match: str | None = Header(default=None, alias="If-Match"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.models.session_v2 import SessionTurnInput
    from app.services.session_human_input import InputRevisionConflict, revise_unbound_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_input_dispatch import dispatch_admitted_input_fast_path
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    await _get_run_session_and_agent(db=db, agent_id=agent_id, session_id=session_id, current_user=current_user)
    authority = await resolve_session_mutation_authority(
        db,
        user=current_user,
        agent_id=agent_id,
        session_id=session_id,
        action="mutate_session_input",
    )
    try:
        receipt = await revise_unbound_human_input(
            db,
            authority=authority,
            input_id=input_id,
            expected_revision=_if_match_revision(if_match),
            content_parts=body.content_parts,
        )
        await db.commit()
        admission = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=input_id,
            worker_id=f"input-revision:{input_id}:{receipt.revision}",
        )
        dispatch = None
        if admission.state == "admitted":
            dispatch = await dispatch_admitted_input_fast_path(
                db,
                admission_id=admission.admission_id,
                worker_id=f"input-revision-dispatch:{input_id}:{receipt.revision}",
            )
    except InputRevisionConflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "input_revision_conflict", "current_revision": exc.current_revision},
        ) from exc
    current = await db.get(SessionTurnInput, input_id)
    if current is None:
        raise RuntimeError("revised input disappeared")
    return {
        "schema": "hive.human_input_receipt",
        "schema_version": 2,
        **asdict(receipt),
        "status": current.status,
        "admission_state": admission.state,
        "reason_code": admission.reason_code,
        "dispatch_status": dispatch.state if dispatch is not None else "not_applicable",
        "dispatch": dict(dispatch.receipt or {}) if dispatch is not None else {},
    }


@router.delete("/{agent_id}/sessions/{session_id}/inputs/{input_id}")
async def cancel_session_human_input(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    input_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.services.session_human_input import InputRevisionConflict, cancel_unbound_human_input
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    await _get_run_session_and_agent(db=db, agent_id=agent_id, session_id=session_id, current_user=current_user)
    authority = await resolve_session_mutation_authority(
        db,
        user=current_user,
        agent_id=agent_id,
        session_id=session_id,
        action="mutate_session_input",
    )
    try:
        receipt = await cancel_unbound_human_input(
            db,
            authority=authority,
            input_id=input_id,
            expected_revision=_if_match_revision(if_match),
        )
        await db.commit()
    except InputRevisionConflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "input_revision_conflict", "current_revision": exc.current_revision},
        ) from exc
    return {"schema": "hive.human_input_receipt", "schema_version": 2, **asdict(receipt)}


@router.post("/{agent_id}/sessions/{session_id}/turns/steer")
async def steer_session_turn(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: SteerSessionTurnIn,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Compatibility wrapper over the canonical Session V2 HumanInput API."""
    await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    active = await get_active_web_chat_run(db=db, agent_id=agent_id, session_id=session_id)
    if active is None:
        raise HTTPException(status_code=404, detail="No active turn to steer")
    run_id = body.expected_run_id or uuid.UUID(str(active["run_id"]))
    turn_id = body.expected_turn_id or str(active.get("turn_id") or f"turn-{run_id.hex}")
    input_id = body.input_id or uuid.uuid4()
    content_parts = list(body.parts or [])
    if not content_parts:
        content_parts = [{"type": "text", "text": body.content}]
    content_parts.extend({"type": "attachment", "attachment": item} for item in body.attachments)
    receipt = await _submit_session_human_input(
        agent_id=agent_id,
        session_id=session_id,
        body=SessionHumanInputIn(
            kind="steer_current_turn",
            input_id=input_id,
            idempotency_key=body.idempotency_key or idempotency_key_header or f"steer:{input_id}",
            content_parts=content_parts,
            expected_turn_id=turn_id,
            expected_run_id=run_id,
            terminal_fallback=body.terminal_fallback,
            permission_mode=body.permission_mode,
        ),
        current_user=current_user,
        db=db,
    )
    return {
        **receipt,
        "queued": receipt,
        "queued_user_message": receipt,
        "steer_strategy": "session_v2_durable_mailbox",
    }


@router.post("/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve")
async def resolve_session_permission(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    permission_request_id: uuid.UUID,
    body: ResolveSessionPermissionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply a typed permission response and resume the original RuntimeTask."""
    await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
    )
    from app.services.session_permission_runtime import resolve_session_tool_permission
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    authority = await resolve_session_mutation_authority(
        db,
        user=current_user,
        agent_id=agent_id,
        session_id=session_id,
        action="respond_tool_permission",
    )
    try:
        receipt = await resolve_session_tool_permission(
            db,
            authority=authority,
            permission_request_id=permission_request_id,
            decision=body.action,
        )
    except IdempotencyConflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_conflict",
                "command_id": str(exc.command_id),
                "receipt_ref": exc.receipt_ref,
            },
        ) from exc
    except ValueError as exc:
        await db.rollback()
        code = str(exc)
        status_code = 404 if code == "pending_session_permission_not_found" else 409
        if code == "destructive_permission_must_be_allow_once":
            status_code = 400
        if code == "tool_permission_request_expired":
            status_code = 410
        raise HTTPException(status_code=status_code, detail={"code": code}) from exc
    from app.services.runtime_task_worker import notify_runtime_task_worker

    if receipt.run_status == "resumable":
        await notify_runtime_task_worker(
            reason="session_permission_resolved",
            runtime_task_id=receipt.run_id,
        )
    return receipt.to_dict()


@router.get("/{agent_id}/threads/{session_id}/read")
async def read_thread(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Thread-style alias for reading a durable session JSON export."""
    session, agent, authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="read_thread",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )
    payload = await build_session_json_export(
        db,
        agent=agent,
        session=session,
        audience="operator" if authority_source == "operator_inspect_grant" else "user",
    )
    await db.commit()
    return payload


@router.get("/{agent_id}/sessions/{session_id}/transcript")
async def get_session_transcript(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    after_sequence: int = 0,
    before_sequence: int | None = None,
    direction: str = "forward",
    limit: int = 200,
    schema_version: int | None = None,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get replayable transcript events for a session.

    This is the durable UI replay surface. `chat_messages` remains a read model
    for compatibility, while transcript events are the ordered event stream.

    Windowing: the default ``after_sequence`` forward contract is unchanged.
    ``direction=backward`` (optionally with ``before_sequence``) reads the
    newest window / pages older history; rows are always returned ascending,
    so clients merge by sequence either way (plan B4).
    """
    if direction not in {"forward", "backward"}:
        raise HTTPException(status_code=400, detail="direction must be 'forward' or 'backward'")
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if operator_view is True
        else check_agent_access(db, current_user, agent_id)
    )
    authority_source = await _authorize_loaded_session(
        db=db,
        session=session,
        agent=agent,
        access_level=access_level,
        current_user=current_user,
        action="read_transcript",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )

    audience = "operator" if authority_source == "operator_inspect_grant" else "user"
    limit = max(1, min(limit, 1000))
    if schema_version not in {None, 2}:
        raise HTTPException(status_code=400, detail="schema_version must be 2 when specified")
    if schema_version == 2:
        from app.services.session_event_contract import serialize_session_event
        from app.services.session_delivery_cursor import (
            SessionDeliveryCursorError,
            load_session_delivery_cursor,
            load_session_delivery_events,
            project_session_event_for_delivery,
        )

        try:
            delivery_cursor = await load_session_delivery_cursor(db, session_id=session_id)
            rows_with_delivery = await load_session_delivery_events(
                db,
                session_id=session_id,
                cursor=delivery_cursor,
                after_sequence=after_sequence,
                before_sequence=before_sequence,
                direction="backward" if direction == "backward" or before_sequence is not None else "forward",
                limit=limit,
            )
            payload = [
                project_session_event_for_delivery(
                    serialize_session_event(event, audience=audience),
                    cursor=delivery_cursor,
                    storage_sequence=int(event.sequence),
                    delivery_sequence=delivery_sequence,
                )
                for event, delivery_sequence in rows_with_delivery
            ]
        except SessionDeliveryCursorError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "session_delivery_cursor_unrecoverable",
                    "retryable": False,
                },
            ) from exc
    else:
        if direction == "backward" or before_sequence is not None:
            stmt = select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id)
            if before_sequence is not None:
                stmt = stmt.where(ChatTranscriptEvent.sequence < before_sequence)
            events_result = await db.execute(stmt.order_by(ChatTranscriptEvent.sequence.desc()).limit(limit))
            rows = list(events_result.scalars().all())
            rows.reverse()
        else:
            events_result = await db.execute(
                select(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.sequence > after_sequence,
                )
                .order_by(ChatTranscriptEvent.sequence.asc())
                .limit(limit)
            )
            rows = list(events_result.scalars().all())
        payload = [_serialize_transcript_event(event, audience=audience) for event in rows]
    await db.commit()
    return payload


async def _cancel_session_run_v2(
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    idempotency_key: str | None,
    current_user: User,
    db: AsyncSession,
) -> dict[str, Any]:
    session, agent, _authority_source = await _get_run_session_and_agent(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        current_user=current_user,
        action="cancel_session_run",
        require_writable=True,
    )
    try:
        receipt = await submit_live_cancel_input(
            db=db,
            agent=agent,
            user=current_user,
            session=session,
            run_id=run_id,
            source="rest_cancel",
            idempotency_key=idempotency_key,
        )
        receipt_status = str(receipt.get("status") or "")
        return {
            **receipt,
            "run_id": str(run_id),
            "accepted": receipt_status in {"accepted", "applying", "applied"},
        }
    except IdempotencyConflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_conflict",
                "command_id": str(exc.command_id),
                "receipt_ref": exc.receipt_ref,
            },
        ) from exc


@router.post("/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel")
async def cancel_session_run(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept a typed cancel request; terminal state waits for worker fence settlement."""

    return await _cancel_session_run_v2(
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        idempotency_key=idempotency_key,
        current_user=current_user,
        db=db,
    )


@router.post("/{agent_id}/threads/{session_id}/turns/{run_id}/interrupt")
async def interrupt_thread_turn(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Thread-style alias over the same typed ControlInput command."""

    return await _cancel_session_run_v2(
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        idempotency_key=idempotency_key,
        current_user=current_user,
        db=db,
    )


@router.delete("/{agent_id}/sessions/{session_id}", status_code=204)
async def delete_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a chat session and its messages. Owner, admin, or creator only."""
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id, ChatSession.agent_id == agent_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    await _authorize_loaded_session(
        db=db,
        session=session,
        agent=agent,
        access_level=access_level,
        current_user=current_user,
        action="delete_session",
        require_writable=True,
    )

    parent_session_id = getattr(session, "parent_session_id", None)
    if parent_session_id is not None:
        from app.runtime.hooks import HookEvent, emit_hook

        remove_hook = await emit_hook(
            HookEvent.WORKTREE_REMOVE,
            evidence_mode="independent",
            agent_id=agent_id,
            session_id=str(session_id),
            source="chat_session_delete",
            metadata={
                "tenant_id": str(getattr(session, "tenant_id", None) or getattr(agent, "tenant_id", None) or "")
                or None,
                "user_id": str(current_user.id),
                "cloud_workspace_kind": "conversation_branch",
                "source_session_id": str(parent_session_id),
                "target_session_id": str(session_id),
                "worktree_uri": f"session://{session_id}/workspace",
            },
        )
        if remove_hook and remove_hook.block:
            raise HTTPException(
                status_code=409,
                detail=remove_hook.reason or "Conversation branch removal blocked by hook",
            )

    # Shared production-shaped deletion: Session V2 rows with inbound
    # transcript-event foreign keys must go before the transcript itself.
    from sqlalchemy.exc import IntegrityError

    from app.services.session_deletion import delete_session_tree

    try:
        await delete_session_tree(db, session)
    except IntegrityError as exc:
        # Intentional restrict edges (workflow promotion proposals, session
        # goals, agent teams, local-agent channel sessions, other sessions
        # branching off this one) stay outside the deletion tree; surface
        # them as an explicit conflict instead of an untyped 500.
        logger.warning("Session deletion blocked: agent_id=%s session_id=%s error=%s", agent_id, session_id, exc.orig)
        raise HTTPException(
            status_code=409,
            detail="Session has dependent records that must be removed first",
        ) from exc
    return None


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def get_session_messages(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    operator_view: bool = Query(default=False),
    operator_reason: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get chat messages for a specific session."""
    # Allow looking up sessions where agent_id OR peer_agent_id matches
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            (ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    agent, access_level = await (
        check_agent_operator_reachability(db, current_user, agent_id)
        if operator_view is True
        else check_agent_access(db, current_user, agent_id)
    )
    await _authorize_loaded_session(
        db=db,
        session=session,
        agent=agent,
        access_level=access_level,
        current_user=current_user,
        action="read_messages",
        operator_view=operator_view is True,
        operator_reason=operator_reason,
    )

    # Query messages by conversation_id only (agent-to-agent uses session_agent_id)
    msgs_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == str(session_id))
        .order_by(ChatMessage.created_at.asc())
        .limit(500)
    )
    messages = msgs_result.scalars().all()
    message_ids = [m.id for m in messages]
    artifacts_by_message: dict[uuid.UUID, list[dict]] = {}
    if message_ids:
        artifacts_result = await db.execute(
            select(ChatArtifact).where(ChatArtifact.message_id.in_(message_ids)).order_by(ChatArtifact.created_at.asc())
        )
        for artifact in artifacts_result.scalars().all():
            artifacts_by_message.setdefault(artifact.message_id, []).append(artifact_part_from_model(artifact))
        artifact_parts = [part for parts in artifacts_by_message.values() for part in parts]
        await _enrich_artifact_agent_names(db, artifact_parts)

    # Resolve sender names for agent sessions
    sender_cache: dict = {}
    if session.source_channel == "agent":
        from app.models.participant import Participant

        for m in messages:
            if m.participant_id and str(m.participant_id) not in sender_cache:
                p_r = await db.execute(select(Participant.display_name).where(Participant.id == m.participant_id))
                sender_cache[str(m.participant_id)] = p_r.scalar_one_or_none() or "Unknown"

    out = []
    for m in messages:
        sender_name = sender_cache.get(str(m.participant_id)) if m.participant_id else None
        artifacts = artifacts_by_message.get(m.id, [])

        if m.role == "tool_call":
            out.append(serialize_chat_message(m, sender_name=sender_name, artifacts=artifacts))
            continue

        # For agent sessions, parse inline tool_code blocks from assistant messages
        if session.source_channel == "agent" and m.role == "assistant" and "```tool_code" in (m.content or ""):
            parts = split_inline_tools(m.content, sender_name=sender_name)
            for part in parts:
                out.append(part)
        else:
            out.append(serialize_chat_message(m, sender_name=sender_name, artifacts=artifacts))

    return out
