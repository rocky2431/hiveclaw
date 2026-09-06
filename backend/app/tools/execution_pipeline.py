"""Typed, staged owner for the governed tool execution lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Callable

from app.core.execution_context import A2A_TOOL_AUTHORITY_FRAME_SCHEMA, A2AToolAuthorityFrame, ExecutionPrincipal
from app.runtime.ccplus_contracts import permission_profile_snapshot, permission_profile_snapshot_hash
from app.services.execution_receipts import canonical_payload_hash

if TYPE_CHECKING:
    from app.tools.service import ApprovalDecisionSet, EventCallback, ToolContentEnvelope


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    tool_name: str
    arguments: dict[str, Any]
    agent_id: Any
    user_id: Any
    execution_identity: Any | None = None
    authority_frame: A2AToolAuthorityFrame | None = None
    tool_call_id: str | None = None
    event_callback: EventCallback | None = None
    delegation_token: Any | None = None
    session_id: str | None = None
    permission_profile: Any | None = None
    turn_id: str | None = None
    runtime_task_id: str | None = None
    budget_run_id: str | None = None
    origin_channel: str | None = None
    round_state: dict[str, Any] | None = None
    t0_refs: tuple[str, ...] = ()
    plan_mode_interactive_available: bool = False
    plan_mode_unattended_available: bool = False
    emit_runtime_hooks: bool = True
    trace_metadata_sink: dict[str, Any] | None = None
    pre_effect_callback: Callable[[dict[str, Any]], Any] | None = None
    workspace_override: Any | None = None
    approval_decision: ApprovalDecisionSet | None = None
    expected_asset_refs: tuple[Any, ...] | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionPorts:
    decision_outcome_type: Any
    extract_error_payload: Callable[..., Any]
    inject_runtime_arguments: Callable[..., Any]
    json: Any
    latest_context_record: Callable[..., Any]
    maybe_await: Callable[..., Any]
    new_tool_call_id: Callable[..., str]
    record_final_decision: Callable[..., Any]
    record_precontext_decision: Callable[..., Any]
    record_execution_frame: Callable[..., Any]
    record_lifecycle: Callable[..., Any]
    renew_runtime_lease: Callable[..., Any]
    resolve_runtime_context: Callable[..., Any]
    tool_result_failed: Callable[..., bool]
    validate_arguments: Callable[..., Any]
    hash_tool_input: Callable[..., str]
    render_tool_error: Callable[..., str]
    asyncio: Any
    inspect: Any
    traceback: Any
    path_type: Any


@dataclass(slots=True)
class _ToolExecutionState:
    service: Any
    request: ToolExecutionRequest
    ports: ToolExecutionPorts
    tool_call_id: str
    arguments: dict[str, Any]
    runtime_context: Any | None = None
    original_arguments: dict[str, Any] | None = None
    governance_context: Any | None = None


@dataclass(frozen=True, slots=True)
class _Stop:
    value: Any


async def run_tool_execution(
    service: Any,
    request: ToolExecutionRequest,
    ports: ToolExecutionPorts,
) -> str | ToolContentEnvelope:
    """Run one governed tool call through four explicit, testable stages."""
    state = _ToolExecutionState(
        service=service,
        request=request,
        ports=ports,
        tool_call_id=request.tool_call_id or ports.new_tool_call_id(),
        arguments=dict(request.arguments or {}),
    )
    for stage in (
        _prepare_runtime_context,
        _apply_exact_secret_preflight,
        _apply_exact_session_scope,
        _apply_plan_mode_and_runtime_arguments,
        _apply_hooks_and_assets,
        _apply_workspace_mutation_path_authority,
        _apply_governance,
    ):
        stopped = await stage(state)
        if stopped is not None:
            return stopped.value
    return await _execute_tool(state)


def _approval_error(state: _ToolExecutionState, *, error_class: str, message: str, hint: str) -> _Stop:
    return _Stop(
        state.ports.render_tool_error(
            tool_name=state.request.tool_name,
            error_class=error_class,
            message=message,
            provider="approval_execution_kernel",
            retryable=False,
            actionable_hint=hint,
        )
    )


def _record_precontext_block(state: _ToolExecutionState, *, outcome: Any, reason: str) -> None:
    request = state.request
    state.ports.record_precontext_decision(
        trace_metadata_sink=request.trace_metadata_sink,
        agent_id=request.agent_id,
        user_id=request.user_id,
        permission_profile=request.permission_profile,
        tool_name=request.tool_name,
        arguments=state.arguments,
        outcome=outcome,
        reason_codes=(reason,),
        tool_call_id=state.tool_call_id,
        runtime_task_id=request.runtime_task_id,
        session_id=request.session_id,
    )


async def _prepare_runtime_context(state: _ToolExecutionState) -> _Stop | None:
    request, ports, service = state.request, state.ports, state.service
    authority_block = _validate_authority_frame(state)
    if authority_block is not None:
        return authority_block
    approval = request.approval_decision
    if approval is not None and (
        approval.tool_name != request.tool_name
        or approval.input_hash != ports.hash_tool_input(request.tool_name, state.arguments)
    ):
        return _approval_error(
            state,
            error_class="approval_payload_mutation",
            message="The approved tool request no longer matches its immutable approval ticket.",
            hint="Submit a new approval request for the exact current action.",
        )
    plan_block = service._interactive_plan_mode_readonly_block(request.tool_name, state.arguments)
    if plan_block:
        _record_precontext_block(state, outcome=ports.decision_outcome_type.DENY, reason="plan_mode_readonly")
        return _Stop(plan_block)
    state.runtime_context = await ports.resolve_runtime_context(
        service.runtime_resolver,
        agent_id=request.agent_id,
        user_id=request.user_id,
        session_id=request.session_id,
        permission_profile=request.permission_profile,
        turn_id=request.turn_id,
        runtime_task_id=request.runtime_task_id,
        budget_run_id=request.budget_run_id,
        origin_channel=request.origin_channel,
        round_state=request.round_state,
        t0_refs=request.t0_refs,
    )
    _configure_runtime_context(state)
    principal = state.runtime_context.execution_principal
    if (
        request.authority_frame is not None
        and request.authority_frame.required
        and (
            not isinstance(principal, ExecutionPrincipal)
            or str(principal.tenant_id) != str(state.runtime_context.tenant_id)
        )
    ):
        _record_precontext_block(
            state,
            outcome=ports.decision_outcome_type.DENY,
            reason="a2a_authority_tenant_mismatch",
        )
        return _Stop(
            ports.render_tool_error(
                tool_name=request.tool_name,
                error_class="authority_context_unavailable",
                message="The A2A authority tenant does not match the resolved tool runtime.",
                provider="a2a_authority_frame",
                retryable=False,
                actionable_hint="Recreate the delegated run from its authenticated parent session.",
            )
        )
    state.original_arguments = dict(state.arguments)
    _record_lifecycle(state, "created")
    return None


async def _apply_exact_secret_preflight(
    state: _ToolExecutionState,
) -> _Stop | None:
    """Stop authority-backed credential bytes before hooks, assets, or logs."""

    boundary = state.runtime_context.exact_secret_boundary
    redaction = boundary.redact_payload_with_evidence(state.original_arguments)
    if not redaction.redacted_count:
        return None

    trace = state.request.trace_metadata_sink
    if trace is not None:
        trace["secret_input_redaction"] = {
            "code": "exact_unauthorized_secret_bytes",
            "redacted_count": redaction.redacted_count,
            "source_refs": list(redaction.matched_refs),
        }

    block = await state.service._preflight_tool_execution(
        state.request.tool_name,
        state.arguments,
        state.runtime_context,
        trace_metadata_sink=trace,
    )
    if block is None:
        from app.tools.decision import ToolBoundaryBlock, ToolDecisionOutcome

        block = ToolBoundaryBlock(
            state.ports.render_tool_error(
                tool_name=state.request.tool_name,
                error_class="unauthorized_secret_bytes",
                message=("The tool request contains exact bytes from a protected credential binding."),
                provider="exact_secret_boundary",
                retryable=False,
                actionable_hint=(
                    "Use the configured credential binding; do not pass raw credential bytes in tool arguments."
                ),
            ),
            outcome=ToolDecisionOutcome.DENY,
            reason_code="unauthorized_secret_bytes",
            status="refuse",
            retryable=False,
        )

    _record_lifecycle(state, "blocked", "exact_secret_preflight")
    _record_final_decision(
        state,
        outcome=state.ports.decision_outcome_type.DENY,
        reasons=("unauthorized_secret_bytes",),
    )
    return _Stop(block)


_EXACT_SESSION_PATH_FIELDS = {
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "glob_search": "root",
    "grep_search": "root",
}


def _canonical_exact_session_root(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or "\\" in raw:
        return None
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or path.parts[0] != "workspace"
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw
    ):
        return None
    return raw


def _scoped_session_path(root: str, value: Any, *, allow_empty: bool) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return root if allow_empty else None
    if "\\" in raw:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != raw:
        return None
    if raw == root or raw.startswith(f"{root}/"):
        return raw
    if path.parts and path.parts[0] == "workspace":
        return None
    return f"{root}/{raw}"


def _exact_scope_stop(state: _ToolExecutionState, *, reason: str, message: str) -> _Stop:
    _record_precontext_block(state, outcome=state.ports.decision_outcome_type.DENY, reason=reason)
    return _Stop(
        state.ports.render_tool_error(
            tool_name=state.request.tool_name,
            error_class="auth_or_permission",
            message=message,
            provider="session_permission_profile",
            retryable=False,
            actionable_hint="Use only the exact tools and evaluation workspace bound to this session.",
            extra={"outcome": "denied", "reason_code": reason},
        )
    )


async def _apply_exact_session_scope(state: _ToolExecutionState) -> _Stop | None:
    profile = permission_profile_snapshot(state.request.permission_profile)
    snapshot = profile.get("capability_policy_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("session_exact_scope") is not True:
        return None
    allowed = profile.get("allowed_tools")
    writable_roots = profile.get("writable_roots")
    readable_roots = profile.get("readable_roots")
    if (
        not isinstance(allowed, list)
        or not allowed
        or any(not isinstance(name, str) or not name.strip() for name in allowed)
        or len(allowed) != len(set(allowed))
        or not isinstance(writable_roots, list)
        or len(writable_roots) != 1
        or readable_roots != writable_roots
    ):
        return _exact_scope_stop(
            state,
            reason="invalid_exact_session_permission_profile",
            message="The exact session permission profile is incomplete or invalid.",
        )
    root = _canonical_exact_session_root(writable_roots[0])
    if root is None:
        return _exact_scope_stop(
            state,
            reason="invalid_exact_session_workspace_root",
            message="The exact session workspace root is invalid.",
        )
    if state.request.tool_name not in allowed:
        return _exact_scope_stop(
            state,
            reason="exact_session_tool_scope_denied",
            message=f"Tool '{state.request.tool_name}' is outside this session's exact tool scope.",
        )
    field = _EXACT_SESSION_PATH_FIELDS.get(state.request.tool_name)
    if field is None:
        return None
    scoped = _scoped_session_path(root, state.arguments.get(field), allow_empty=field == "root")
    if scoped is None:
        return _exact_scope_stop(
            state,
            reason="exact_session_workspace_scope_denied",
            message="The requested path is outside this session's exact workspace root.",
        )
    if state.request.tool_name == "glob_search":
        pattern = str(state.arguments.get("pattern") or "")
        pattern_path = PurePosixPath(pattern)
        if (
            not pattern
            or "\\" in pattern
            or pattern_path.is_absolute()
            or any(part == ".." for part in pattern_path.parts)
        ):
            return _exact_scope_stop(
                state,
                reason="exact_session_workspace_scope_denied",
                message="The glob pattern may not escape this session's exact workspace root.",
            )
    state.arguments[field] = scoped
    return None


async def _apply_plan_mode_and_runtime_arguments(
    state: _ToolExecutionState,
) -> _Stop | None:
    """Apply confirmation policy, then add trusted runtime-owned arguments."""

    request, ports, service = state.request, state.ports, state.service
    approval = request.approval_decision
    authorization: dict[str, Any] = {}
    plan_block = await service._plan_mode_gate_block(
        request.tool_name,
        state.arguments,
        agent_id=request.agent_id,
        user_id=request.user_id,
        session_id=request.session_id,
        runtime_task_id=request.runtime_task_id,
        evidence_id=f"tool:{state.tool_call_id}",
        authorization_sink=authorization,
        plan_mode_interactive_available=request.plan_mode_interactive_available,
        plan_mode_unattended_available=request.plan_mode_unattended_available,
        consumed_plan_authorization=(approval.plan_authorization if approval is not None else None),
        approval_tenant_id=str(getattr(approval, "tenant_id", "")) if approval is not None else None,
    )
    if plan_block:
        _record_precontext_block(
            state,
            outcome=ports.decision_outcome_type.REQUIRE_APPROVAL,
            reason="plan_confirmation_required",
        )
        return _Stop(plan_block)
    if authorization:
        state.arguments["_plan_authorization"] = authorization
        if isinstance(request.trace_metadata_sink, dict):
            request.trace_metadata_sink["plan_authorization"] = dict(authorization)

    state.arguments = ports.inject_runtime_arguments(
        request.tool_name,
        state.arguments,
        state.runtime_context,
    )
    if approval is not None and approval.input_hash != ports.hash_tool_input(
        request.tool_name,
        state.arguments,
    ):
        _record_lifecycle(state, "blocked", "approval_payload_mutation")
        return _approval_error(
            state,
            error_class="approval_payload_mutation",
            message="Runtime context injection changed the immutable approved request.",
            hint="Submit a new approval request from the current runtime context.",
        )
    return None


def _configure_runtime_context(state: _ToolExecutionState) -> None:
    request, context = state.request, state.runtime_context
    context.delegation_token = request.delegation_token
    if request.execution_identity is not None:
        context.execution_identity = request.execution_identity
    authority_frame = request.authority_frame
    principal = authority_frame.principal if authority_frame is not None else None
    if isinstance(principal, Mapping):
        principal = ExecutionPrincipal.from_evidence(principal)
    context.execution_principal = principal
    context.authority_snapshot_hash = authority_frame.capability_snapshot_hash if authority_frame else None
    context.authority_policy_hash = authority_frame.policy_snapshot_hash if authority_frame else None
    context.authority_frame_schema = authority_frame.schema if authority_frame else None
    context.authority_frame_required = authority_frame is not None
    context.authority_trace_id = authority_frame.trace_id if authority_frame else None
    context.authority_parent_session_id = authority_frame.parent_session_id if authority_frame else None
    context.authority_root_runtime_task_id = authority_frame.root_runtime_task_id if authority_frame else None
    context.authority_budget_run_id = authority_frame.budget_run_id if authority_frame else None
    context.authority_delegation_id = authority_frame.delegation_id if authority_frame else None
    context.authority_sandbox_profile = authority_frame.sandbox_profile if authority_frame else None
    context.authority_approval_policy = authority_frame.approval_policy if authority_frame else None
    context.approval_decision = request.approval_decision
    context.emit_runtime_hooks = request.emit_runtime_hooks
    context.plan_mode_interactive_available = request.plan_mode_interactive_available
    context.plan_mode_unattended_available = request.plan_mode_unattended_available
    if request.workspace_override is not None:
        context.workspace = state.ports.path_type(request.workspace_override)


def _valid_sha256(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return len(normalized) == 64 and all(char in "0123456789abcdef" for char in normalized)


def _authority_principal(frame: A2AToolAuthorityFrame) -> ExecutionPrincipal | None:
    principal = frame.principal
    try:
        if isinstance(principal, Mapping):
            principal = ExecutionPrincipal.from_evidence(principal)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return principal if isinstance(principal, ExecutionPrincipal) else None


def _identity_snapshot(value: Any | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        source = value
    else:
        source = {
            "identity_type": getattr(value, "identity_type", None),
            "identity_id": getattr(value, "identity_id", None),
            "label": getattr(value, "label", None),
        }
    identity_type = str(source.get("identity_type") or "").strip()
    if not identity_type:
        return None
    identity_id = source.get("identity_id")
    return {
        "identity_type": identity_type,
        "identity_id": str(identity_id) if identity_id else None,
        "label": source.get("label"),
    }


def _authority_snapshot_failure(
    request: ToolExecutionRequest,
    frame: A2AToolAuthorityFrame,
    principal: ExecutionPrincipal,
) -> tuple[str, str] | None:
    snapshot = dict(frame.capability_snapshot) if isinstance(frame.capability_snapshot, Mapping) else None
    if snapshot is None:
        return "a2a_authority_snapshot_missing", "The A2A authority snapshot is missing."
    if canonical_payload_hash(snapshot) != frame.capability_snapshot_hash:
        return "a2a_authority_snapshot_drift", "The A2A authority snapshot no longer matches its receipt."
    profile = permission_profile_snapshot(frame.permission_profile)
    if permission_profile_snapshot(request.permission_profile) != profile:
        return "a2a_authority_policy_drift", "The A2A permission profile changed before the tool effect."
    if snapshot.get("permission_profile") != profile:
        return "a2a_authority_snapshot_drift", "The A2A permission profile is not bound to the authority snapshot."
    if snapshot.get("execution_principal") != principal.to_evidence():
        return "a2a_execution_principal_drift", "The A2A principal is not bound to the authority snapshot."
    expected_bindings = {
        "tenant_id": str(principal.tenant_id),
        "owner_id": str(request.user_id or ""),
        "target_agent_id": str(request.agent_id or ""),
        "session_id": str(frame.session_id or ""),
        "parent_session_id": str(frame.parent_session_id or ""),
        "runtime_task_id": str(frame.runtime_task_id or ""),
        "root_runtime_task_id": str(frame.root_runtime_task_id or ""),
        "budget_run_id": str(frame.budget_run_id or ""),
        "trace_id": str(frame.trace_id or ""),
    }
    for key, expected in expected_bindings.items():
        if str(snapshot.get(key) or "") != expected:
            return "a2a_authority_binding_drift", f"The A2A authority binding '{key}' changed before effect."
    if _identity_snapshot(frame.execution_identity) != snapshot.get("execution_identity"):
        return "a2a_execution_identity_drift", "The A2A execution identity changed before the tool effect."
    if _identity_snapshot(request.execution_identity) != _identity_snapshot(frame.execution_identity):
        return "a2a_execution_identity_drift", "The tool request did not consume the framed execution identity."
    if str(request.session_id or "") != str(frame.session_id or ""):
        return "a2a_authority_binding_drift", "The tool request session differs from the A2A authority frame."
    if str(request.runtime_task_id or "") != str(frame.runtime_task_id or ""):
        return "a2a_authority_binding_drift", "The tool request task differs from the A2A authority frame."
    if str(request.budget_run_id or "") != str(frame.budget_run_id or ""):
        return "a2a_authority_binding_drift", "The tool request budget differs from the A2A authority frame."
    if str(principal.root_runtime_task_id or "") != str(frame.root_runtime_task_id or ""):
        return "a2a_authority_binding_drift", "The root task differs from the A2A execution principal."
    if not str(frame.trace_id or "").strip():
        return "a2a_authority_binding_drift", "The A2A authority frame has no trace reference."
    if str(profile.get("sandbox") or "") != str(frame.sandbox_profile or ""):
        return "a2a_sandbox_profile_drift", "The A2A sandbox profile changed before the tool effect."
    if str(profile.get("approval_policy") or "") != str(frame.approval_policy or ""):
        return "a2a_approval_policy_drift", "The A2A approval policy changed before the tool effect."
    token = frame.delegation_token
    if token is not None:
        if str(getattr(token, "parent_agent_id", "")) != str(snapshot.get("source_agent_id") or ""):
            return "a2a_delegation_token_drift", "The delegation token parent differs from the authority frame."
        if str(getattr(token, "child_agent_id", "")) != str(snapshot.get("target_agent_id") or ""):
            return "a2a_delegation_token_drift", "The delegation token child differs from the authority frame."
        if frame.delegation_id and str(getattr(token, "delegation_id", "")) != frame.delegation_id:
            return "a2a_delegation_token_drift", "The delegation token id differs from the authority frame."
    elif snapshot.get("interaction_type") == "delegation":
        return "a2a_delegation_token_missing", "The delegated A2A effect has no delegation token."
    return None


def _authority_frame_failure(
    request: ToolExecutionRequest,
    frame: A2AToolAuthorityFrame,
    principal: ExecutionPrincipal | None,
) -> tuple[str, str] | None:
    if frame.required is not True:
        return "a2a_authority_frame_required_invalid", "The A2A authority frame cannot disable effect validation."
    if frame.schema != A2A_TOOL_AUTHORITY_FRAME_SCHEMA:
        return "a2a_authority_frame_version_invalid", "The A2A authority frame version is missing or unsupported."
    if principal is None:
        return "a2a_execution_principal_missing", "The A2A execution principal is missing or invalid."
    if str(principal.source_agent_id) != str(request.agent_id):
        return "a2a_execution_principal_agent_mismatch", "The A2A principal does not match the target Agent."
    if str(principal.requester_user_id or "") != str(request.user_id or ""):
        return "a2a_execution_principal_requester_mismatch", "The A2A principal does not match the requester."
    if not principal.root_session_id:
        return "a2a_root_session_missing", "The A2A execution principal has no root session authority."
    if not _valid_sha256(frame.capability_snapshot_hash):
        return "a2a_authority_snapshot_invalid", "The A2A authority snapshot hash is missing or invalid."
    if not _valid_sha256(frame.policy_snapshot_hash):
        return "a2a_authority_policy_invalid", "The A2A permission policy hash is missing or invalid."
    if frame.policy_snapshot_hash != permission_profile_snapshot_hash(frame.permission_profile):
        return "a2a_authority_policy_drift", "The A2A permission profile no longer matches its receipt."
    snapshot_failure = _authority_snapshot_failure(request, frame, principal)
    if snapshot_failure is not None:
        return snapshot_failure
    denied_actions = {
        str(action).strip()
        for action in permission_profile_snapshot(frame.permission_profile).get("denied_actions", ())
        if str(action).strip()
    }
    if request.tool_name in denied_actions:
        return (
            "a2a_parent_effect_denied",
            f"The parent A2A permission profile explicitly denies tool '{request.tool_name}'.",
        )
    return None


def _validate_authority_frame(state: _ToolExecutionState) -> _Stop | None:
    request, ports = state.request, state.ports
    frame = request.authority_frame
    if frame is None:
        return None
    failure = _authority_frame_failure(request, frame, _authority_principal(frame))
    if failure is None:
        return None
    reason, message = failure
    _record_precontext_block(
        state,
        outcome=ports.decision_outcome_type.DENY,
        reason=reason,
    )
    return _Stop(
        ports.render_tool_error(
            tool_name=request.tool_name,
            error_class="authority_context_unavailable",
            message=message,
            provider="a2a_authority_frame",
            retryable=False,
            actionable_hint="Recreate the delegated run from its authenticated parent session.",
            extra={"outcome": "unavailable", "reason_code": reason},
        )
    )


def _record_lifecycle(state: _ToolExecutionState, status: str, *decisions: str) -> None:
    state.ports.record_lifecycle(
        state.runtime_context,
        tool_call_id=state.tool_call_id,
        tool_name=state.request.tool_name,
        state=status,
        original_arguments=state.original_arguments or dict(state.arguments),
        effective_arguments=dict(state.arguments),
        governance_decisions=decisions,
    )


def _record_final_decision(
    state: _ToolExecutionState,
    *,
    outcome: Any,
    reasons: tuple[str, ...],
) -> None:
    state.ports.record_final_decision(
        trace_metadata_sink=state.request.trace_metadata_sink,
        runtime_context=state.runtime_context,
        tool_name=state.request.tool_name,
        arguments=state.arguments,
        outcome=outcome,
        reason_codes=reasons,
        tool_call_id=state.tool_call_id,
        governance_context=state.governance_context,
    )


async def _apply_hooks_and_assets(state: _ToolExecutionState) -> _Stop | None:
    request, ports, service = state.request, state.ports, state.service
    if request.emit_runtime_hooks:
        hook_result = await service._emit_pre_tool_hook(
            request.tool_name,
            state.arguments,
            state.runtime_context,
            tool_call_id=state.tool_call_id,
            source="tool_runtime_service",
        )
        if hook_result and hook_result.block:
            _record_lifecycle(state, "blocked", "pre_tool_hook_block")
            _record_final_decision(
                state,
                outcome=ports.decision_outcome_type.DENY,
                reasons=("pre_tool_hook_block",),
            )
            if request.approval_decision is not None:
                return _approval_error(
                    state,
                    error_class="approval_hook_block",
                    message=(
                        "Approved execution was blocked by the current PRE_TOOL_USE hook: "
                        f"{hook_result.reason or 'policy'}"
                    ),
                    hint="Review the changed hook or submit a new approval request.",
                )
            return _Stop("Blocked by hook: " + (hook_result.reason or "policy"))
        if hook_result and hook_result.modified_args is not None:
            modified = dict(hook_result.modified_args)
            approval = request.approval_decision
            if approval is not None and approval.input_hash != ports.hash_tool_input(request.tool_name, modified):
                return _approval_error(
                    state,
                    error_class="approval_payload_mutation",
                    message="PRE_TOOL_USE cannot change an immutable approved request.",
                    hint="Submit a new approval request for the modified action.",
                )
            state.arguments = modified
    validation_block = ports.validate_arguments(request.tool_name, state.arguments)
    if validation_block:
        _record_lifecycle(state, "blocked", "validate_input_block")
        _record_final_decision(
            state,
            outcome=ports.decision_outcome_type.DENY,
            reasons=("invalid_tool_arguments",),
        )
        return _Stop(validation_block)
    _record_lifecycle(state, "validated")
    asset_block = await _resolve_assets(state)
    if asset_block is not None:
        return asset_block
    l2_block = await service._l2_extension_policy_block(request.tool_name, state.runtime_context)
    if l2_block:
        _record_lifecycle(state, "blocked", "l2_policy_block")
        _record_final_decision(
            state,
            outcome=ports.decision_outcome_type.DENY,
            reasons=("l2_policy_block",),
        )
        return _Stop(l2_block)
    return None


async def _resolve_assets(state: _ToolExecutionState) -> _Stop | None:
    from app.services.ai_asset_resolution import resolved_asset_refs_match, resolve_tool_asset_refs

    request, service = state.request, state.service
    resolver = service.asset_ref_resolver or resolve_tool_asset_refs
    refs = await state.ports.maybe_await(
        resolver(tool_name=request.tool_name, arguments=dict(state.arguments), context=state.runtime_context)
    )
    state.runtime_context.resolved_asset_refs = tuple(refs or ())
    if request.expected_asset_refs is None or resolved_asset_refs_match(
        request.expected_asset_refs,
        state.runtime_context.resolved_asset_refs,
    ):
        return None
    _record_lifecycle(state, "blocked", "approval_asset_revision_drift")
    return _approval_error(
        state,
        error_class="approval_asset_revision_drift",
        message="The approved AI asset revision is no longer the active resolved revision.",
        hint="Review the new asset revision and submit a new approval request.",
    )


# Each field maps to (action, optionality). ``None`` marks the required
# ``path`` field; optional fields mirror the final handlers' fallback rules:
# ``template_path`` is honored only when truthy, and ``output_path`` falls
# back to the source path unless it names a different target.
_WORKSPACE_MUTATION_PATH_AUTHORITY: dict[str, tuple[tuple[tuple[str, str, str | None], ...], bool]] = {
    "write_file": ((("path", "write", None),), False),
    "edit_file": ((("path", "write", None),), False),
    "delete_file": ((("path", "delete", None),), False),
    "office_document_create": ((("path", "create", None), ("template_path", "read", "truthy")), True),
    "office_document_apply": ((("path", "write", None), ("output_path", "write", "differ_source")), True),
}

_FS_WRITE_TOOL_NAME = "fs_write"


def _fs_write_action(arguments: dict[str, Any]) -> str:
    # The unified facade keeps write/edit/delete semantics; edit and the
    # default write mode both authorize as `write` in the final handlers.
    mode = str(arguments.get("mode") or "write").strip().lower()
    return "delete" if mode == "delete" else "write"


async def _apply_workspace_mutation_path_authority(state: _ToolExecutionState) -> _Stop | None:
    """Deny deterministic workspace path escapes before governance runs.

    The final filesystem handler re-runs the same check (defense in depth), but
    a denial that needs no external authority must not be maskable by a
    governance dependency outage, so it is decided here as well.
    """

    tool_name = state.request.tool_name
    if tool_name == _FS_WRITE_TOOL_NAME:
        fields = (("path", _fs_write_action(state.arguments), None),)
        require_user_workspace = False
    else:
        entry = _WORKSPACE_MUTATION_PATH_AUTHORITY.get(tool_name)
        if entry is None:
            return None
        fields, require_user_workspace = entry
    from app.services.workspace_resource_authority import (
        WorkspaceAuthorityError,
        authorize_workspace_tool_path,
    )

    context = state.runtime_context
    for field, action, optionality in fields:
        path = state.arguments.get(field)
        if path is None:
            continue
        text = str(path)
        # Optional presence mirrors the final handlers' plain truthiness:
        # ``""`` falls back to the default path, but whitespace-only values
        # are still honored as distinct targets and must be authorized here.
        if optionality == "truthy" and not text:
            continue
        if optionality == "differ_source" and (not text or text == str(state.arguments.get("path", ""))):
            continue
        try:
            authorize_workspace_tool_path(
                context.workspace,
                context.workspace_authority_scope,
                text,
                action=action,
                require_user_workspace=require_user_workspace,
            )
        except WorkspaceAuthorityError as exc:
            _record_lifecycle(state, "blocked", "workspace_path_authority")
            _record_final_decision(
                state,
                outcome=state.ports.decision_outcome_type.DENY,
                reasons=(exc.code,),
            )
            return _Stop(
                state.ports.render_tool_error(
                    tool_name=state.request.tool_name,
                    error_class="auth_or_permission",
                    message=exc.message,
                    provider="workspace_path_authority",
                    retryable=False,
                    actionable_hint="Use a canonical workspace-relative path and do not use `..` segments.",
                    extra={"outcome": "denied", "reason_code": exc.code},
                )
            )
    return None


async def _apply_governance(state: _ToolExecutionState) -> _Stop | None:
    from app.tools.decision import ToolDecisionOutcome, boundary_block_from_machine_value

    request, ports, service = state.request, state.ports, state.service
    kwargs: dict[str, Any] = {
        "runtime_context": state.runtime_context,
        "tool_name": request.tool_name,
        "arguments": state.arguments,
        "delegation_token": request.delegation_token,
    }
    try:
        params = ports.inspect.signature(service.governance_resolver.build_context).parameters
        accepts_kwargs = any(param.kind == ports.inspect.Parameter.VAR_KEYWORD for param in params.values())
    except (TypeError, ValueError):
        params, accepts_kwargs = {}, False
    if accepts_kwargs or "tool_call_id" in params:
        kwargs["tool_call_id"] = state.tool_call_id
    state.governance_context = await service.governance_resolver.build_context(**kwargs)
    dependencies = service.governance_resolver.build_dependencies()
    block = await ports.maybe_await(
        service.governance_runner(
            state.governance_context,
            dependencies,
            event_callback=request.event_callback,
        )
    )
    if block:
        _record_lifecycle(state, "blocked", "governance_block")
        typed_block = boundary_block_from_machine_value(block)
        if typed_block is not None:
            outcome = typed_block.outcome
            reason_code = typed_block.reason_code
        else:
            # A legacy/custom runner that returns only prose has violated the
            # boundary contract.  Keep the effect fail-closed, but record an
            # unavailable dependency instead of inventing denial/approval from
            # natural-language substrings.
            outcome = ToolDecisionOutcome.UNAVAILABLE
            reason_code = "untyped_governance_block"
        _record_final_decision(state, outcome=outcome, reasons=(reason_code,))
        return _Stop(block)
    _record_lifecycle(state, "governed")
    preflight_block = await service._preflight_tool_execution(
        request.tool_name,
        state.arguments,
        state.runtime_context,
        trace_metadata_sink=request.trace_metadata_sink,
        secret_check_payload=state.original_arguments,
    )
    if preflight_block:
        typed_block = boundary_block_from_machine_value(preflight_block)
        if typed_block is not None:
            outcome = typed_block.outcome
            reason_code = typed_block.reason_code
        else:
            outcome = ToolDecisionOutcome.UNAVAILABLE
            reason_code = "untyped_preflight_block"
        if outcome == ToolDecisionOutcome.REQUIRE_APPROVAL:
            from app.tools.governance import request_action_preflight_approval

            approval_block = await request_action_preflight_approval(
                state.governance_context,
                dependencies,
                reason_code=reason_code,
                event_callback=request.event_callback,
            )
            if approval_block is None:
                _record_lifecycle(state, "preflight", "preflight_preauthorized")
                _record_final_decision(
                    state,
                    outcome=ports.decision_outcome_type.ALLOW,
                    reasons=("governance_allow", "preflight_preauthorized"),
                )
                return None
            preflight_block = approval_block
            outcome = approval_block.outcome
            reason_code = approval_block.reason_code
        _record_lifecycle(state, "blocked", "preflight_block")
        _record_final_decision(state, outcome=outcome, reasons=(reason_code,))
        return _Stop(preflight_block)
    _record_lifecycle(state, "preflight")
    _record_final_decision(
        state,
        outcome=ports.decision_outcome_type.ALLOW,
        reasons=("governance_allow", "preflight_allow"),
    )
    return None


async def _execute_tool(state: _ToolExecutionState) -> Any:
    from app.tools.registry import tool_execution_policy

    request, ports, service = state.request, state.ports, state.service
    timeout_seconds = tool_execution_policy(request.tool_name).timeout_seconds
    await ports.renew_runtime_lease()
    if request.pre_effect_callback is not None:
        # Governance/preflight and the worker lease are already valid. The
        # callback is the durable authority fence; its failure must propagate
        # before executor entry, not be rewritten as an executor failure.
        await ports.maybe_await(
            request.pre_effect_callback(
                {
                    "tool_call_id": state.tool_call_id,
                    "tool_name": request.tool_name,
                    "arguments": dict(state.arguments),
                    "decision_id": (request.trace_metadata_sink or {}).get("decision_id"),
                    "idempotency_key": (request.trace_metadata_sink or {}).get("idempotency_key"),
                }
            )
        )
    try:
        _record_lifecycle(state, "executing")
        result = await ports.asyncio.wait_for(
            service.execute_with_context(
                request.tool_name,
                state.arguments,
                state.runtime_context,
                trace_metadata_sink=request.trace_metadata_sink,
            ),
            timeout=timeout_seconds,
        )
        result_text = str(result)
        error_payload = ports.extract_error_payload(result_text)
        failed = ports.tool_result_failed(result)
        if failed and error_payload is None:
            error_payload = {"error_class": "legacy_tool_error", "message": result_text, "retryable": False}
        _record_lifecycle(state, "failed" if failed else "completed")
        await _log_execution_result(state, result_text, error_payload)
        rewritten = await _run_result_hooks(state, result_text, failed)
        return rewritten.value if rewritten is not None else result
    except ports.asyncio.TimeoutError:
        return await _handle_execution_failure(state, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return await _handle_execution_failure(state, exception=exc)


async def _log_execution_result(
    state: _ToolExecutionState,
    result_text: str,
    error_payload: dict[str, Any] | None,
) -> None:
    service, request, ports = state.service, state.request, state.ports
    if not service.activity_logger:
        return
    approval = request.approval_decision
    approval_detail = (
        {
            "approved": True,
            "requested_by_user_id": str(approval.requested_by_user_id),
            "approved_by_user_id": str(approval.approved_by_user_id),
            "approval_id": str(approval.approval_id),
            "input_hash": approval.input_hash,
            "policy_snapshot_hash": approval.policy_snapshot_hash,
            "decision_id": approval.decision_id,
        }
        if approval is not None
        else {}
    )
    await ports.maybe_await(
        service.activity_logger(
            request.agent_id,
            "tool_call_approved" if approval is not None else "tool_call",
            # The summary is the user-facing sentence; the raw result payload
            # (JSON, internal IDs) stays in the structured detail below so
            # normal-user surfaces never render it inline.
            f"{'Approved-executed' if approval is not None else 'Called tool'} {request.tool_name}",
            tenant_id=state.runtime_context.tenant_id,
            owner_user_id=state.runtime_context.user_id,
            root_session_id=state.runtime_context.session_id,
            detail={
                "tool": request.tool_name,
                "backend": service.backend.name if service.backend else "unknown",
                "args": {
                    key: (
                        ports.json.dumps(value, ensure_ascii=False, default=str)
                        if isinstance(value, (dict, list))
                        else str(value)
                    )
                    for key, value in state.arguments.items()
                },
                "result": result_text,
                "tool_call_lifecycle": ports.latest_context_record(state.runtime_context, "tool_lifecycle_records"),
                "tool_execution_frame": ports.latest_context_record(state.runtime_context, "tool_execution_frames"),
                **approval_detail,
            },
        )
    )
    if error_payload:
        await ports.maybe_await(
            service.activity_logger(
                request.agent_id,
                "error",
                f"Tool {request.tool_name} failed: {error_payload.get('error_class', 'unknown')}",
                tenant_id=state.runtime_context.tenant_id,
                owner_user_id=state.runtime_context.user_id,
                root_session_id=state.runtime_context.session_id,
                detail=error_payload,
            )
        )


async def _run_result_hooks(state: _ToolExecutionState, result_text: str, failed: bool) -> _Stop | None:
    request, service, ports = state.request, state.service, state.ports
    if not request.emit_runtime_hooks:
        return None
    if failed:
        await service._emit_tool_failure_hook(
            request.tool_name,
            state.arguments,
            result_text,
            state.runtime_context,
            tool_call_id=state.tool_call_id,
            source="tool_runtime_service",
        )
        return None
    hook_result = await service._emit_post_tool_hook(
        request.tool_name,
        state.arguments,
        result_text,
        state.runtime_context,
        tool_call_id=state.tool_call_id,
        source="tool_runtime_service",
    )
    if hook_result and hook_result.output_rewrite is not None:
        rewrite = hook_result.output_rewrite
        rewrite, _redaction = service._redact_runtime_egress(
            rewrite,
            context=state.runtime_context,
            surface="post_tool_hook_rewrite",
            trace_metadata_sink=request.trace_metadata_sink,
        )
        return _Stop(
            rewrite if isinstance(rewrite, str) else ports.json.dumps(rewrite, ensure_ascii=False, sort_keys=True)
        )
    return None


async def _handle_execution_failure(
    state: _ToolExecutionState,
    *,
    timeout_seconds: float | None = None,
    exception: Exception | None = None,
) -> str:
    request, ports, service = state.request, state.ports, state.service
    is_timeout = timeout_seconds is not None
    if exception is not None:
        ports.traceback.print_exc()
    error_class = "timeout" if is_timeout else "tool_execution_error"
    message = (
        f"{request.tool_name} exceeded the {int(timeout_seconds or 0)} second time limit."
        if is_timeout
        else f"{request.tool_name} failed with {type(exception).__name__}: {str(exception)}"
    )
    rendered = ports.render_tool_error(
        tool_name=request.tool_name,
        error_class=error_class,
        message=message,
        provider="runtime",
        retryable=is_timeout,
        actionable_hint=(
            "Try a simpler request, smaller input, or a more targeted operation."
            if is_timeout
            else "Check tool arguments and try again with simpler or better-scoped input."
        ),
    )
    _record_lifecycle(state, "failed", error_class)
    ports.record_execution_frame(
        state.runtime_context,
        tool_call_id=state.tool_call_id,
        tool_name=request.tool_name,
        executor=service.backend.name if service.backend else "unknown",
        arguments=state.arguments,
        status="failed",
        result={"error": error_class if is_timeout else type(exception).__name__, "message": message},
        trace_metadata_sink=request.trace_metadata_sink,
    )
    if request.emit_runtime_hooks:
        await service._emit_tool_failure_hook(
            request.tool_name,
            state.arguments,
            rendered,
            state.runtime_context,
            tool_call_id=state.tool_call_id,
            source="tool_runtime_service",
        )
    await _log_failure_activity(state, error_class=error_class, retryable=is_timeout, exception=exception)
    return rendered


async def _log_failure_activity(
    state: _ToolExecutionState,
    *,
    error_class: str,
    retryable: bool,
    exception: Exception | None,
) -> None:
    service, request, ports = state.service, state.request, state.ports
    if not service.activity_logger:
        return
    detail = {
        "tool_name": request.tool_name,
        "error_class": error_class,
        "retryable": retryable,
        "provider": "runtime",
    }
    if exception is not None:
        detail["exception_type"] = type(exception).__name__
    await ports.maybe_await(
        service.activity_logger(
            request.agent_id,
            "error",
            f"Tool {request.tool_name} {'timed out' if retryable else f'failed with {type(exception).__name__}'}",
            tenant_id=state.runtime_context.tenant_id,
            owner_user_id=state.runtime_context.user_id,
            root_session_id=state.runtime_context.session_id,
            detail=detail,
        )
    )
