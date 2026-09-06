"""Pre-governance workspace path-authority denial for core file mutation tools.

P01-MAIN authority-negative regression: a standard Session ``write_file`` with
a parent-traversal path must receive a typed non-retryable ``auth_or_permission``
denial at the shared execution-pipeline boundary — before governance, so a
governance dependency outage can never mask a deterministic path-escape denial.
The denial is recorded as a normal post-context boundary block: the final trace
decision carries the resolved runtime tenant and the last lifecycle is
``blocked`` with the workspace-path decision.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.workspace_resource_authority import WorkspaceAuthorityScope
from app.tools.runtime import ToolExecutionContext
from app.tools.service import ToolRuntimeService


class _FakeRuntimeResolver:
    def __init__(self, context):
        self.context = context

    async def resolve(self, **_kwargs):
        return self.context


class _FakeGovernanceResolver:
    def __init__(self):
        self.context_calls = 0

    async def build_context(self, **_kwargs):
        self.context_calls += 1
        return SimpleNamespace()

    def build_dependencies(self):
        return SimpleNamespace()


class _ExplodingRegistry:
    def __init__(self):
        self.calls = 0

    def try_execute(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("registry executor reached for a denied path")


async def _unreachable_governance(_context, _deps, *, event_callback=None):
    raise AssertionError("governance runner reached for a denied path")


def _service(context) -> ToolRuntimeService:
    return ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(),
        registry=_ExplodingRegistry(),
        ensure_registry=lambda: None,
        governance_runner=_unreachable_governance,
        fallback_executor=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("fallback executor reached for a denied path")
        ),
        direct_fallback_executor=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("direct fallback executor reached for a denied path")
        ),
        activity_logger=None,
    )


def _context(tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=tmp_path / "agent-ws",
        session_id="session-p01-negative",
    )


def _error_payload(result: str) -> dict:
    start = result.find("<tool_error>")
    assert start != -1, result
    payload = result[start + len("<tool_error>") : result.rfind("</tool_error>")]
    return json.loads(payload)


async def _execute_and_assert_denied(
    service,
    context,
    tool_name: str,
    arguments: dict,
    *,
    reason_code: str,
) -> dict:
    trace: dict = {}
    result = await service.execute(
        tool_name,
        arguments,
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id=context.session_id,
        trace_metadata_sink=trace,
    )
    payload = _error_payload(result)
    assert payload["error_class"] == "auth_or_permission"
    assert payload["reason_code"] == reason_code
    assert payload.get("outcome") == "denied"
    assert payload.get("retryable") is False
    decision = trace["tool_decision"]
    assert decision["outcome"] == "deny"
    assert decision["reason_codes"] == (reason_code,)
    assert decision["tenant_id"] == context.tenant_id
    lifecycle = context.tool_lifecycle_records[-1]
    assert lifecycle["lifecycle_state"] == "blocked"
    assert "workspace_path_authority" in lifecycle["governance_decisions"]
    assert service.governance_resolver.context_calls == 0
    assert service.registry.calls == 0
    return payload


@pytest.mark.asyncio
async def test_write_file_traversal_denied_before_governance(tmp_path):
    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)

    result = await service.execute(
        "write_file",
        {"path": "../P01-MAIN-NEGATIVE-CEDAR-ELM-20260907.md", "content": "must not be written"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )

    payload = _error_payload(result)
    assert payload["error_class"] == "auth_or_permission"
    assert payload["reason_code"] == "workspace_resource_path_escape"
    assert payload.get("outcome") == "denied"
    assert payload.get("retryable") is False
    assert not (tmp_path / "P01-MAIN-NEGATIVE-CEDAR-ELM-20260907.md").exists()
    assert not (context.workspace / "..").joinpath("P01-MAIN-NEGATIVE-CEDAR-ELM-20260907.md").resolve().exists()
    lifecycle = context.tool_lifecycle_records[-1]
    assert lifecycle["lifecycle_state"] == "blocked"
    assert "workspace_path_authority" in lifecycle["governance_decisions"]


@pytest.mark.asyncio
async def test_edit_file_traversal_denied_before_governance(tmp_path):
    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)

    await _execute_and_assert_denied(
        service,
        context,
        "edit_file",
        {"path": "../escape.txt", "old_text": "a", "new_text": "b"},
        reason_code="workspace_resource_path_escape",
    )
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_delete_file_traversal_denied_before_governance(tmp_path):
    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)

    await _execute_and_assert_denied(
        service,
        context,
        "delete_file",
        {"path": "../escape.txt"},
        reason_code="workspace_resource_path_escape",
    )
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_write_file_absolute_path_denied_before_governance(tmp_path):
    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)

    result = await service.execute(
        "write_file",
        {"path": "/etc/p01-negative-escape.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )

    payload = _error_payload(result)
    assert payload["error_class"] == "auth_or_permission"
    assert payload["reason_code"] == "workspace_resource_path_escape"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("office_document_create", {"path": "", "kind": "docx"}),
        ("office_document_apply", {"path": "", "operations": [{}]}),
        # Whitespace-only optional paths are truthy in the final handlers and
        # must be authority-denied here, not silently skipped.
        ("office_document_create", {"path": "workspace/ok.docx", "kind": "docx", "template_path": "  "}),
        ("office_document_apply", {"path": "workspace/ok.docx", "operations": [{}], "output_path": "  "}),
    ],
)
async def test_office_required_or_whitespace_path_denied_before_governance(tmp_path, tool_name, arguments):
    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)

    await _execute_and_assert_denied(
        service,
        context,
        tool_name,
        arguments,
        reason_code="workspace_resource_path_required",
    )


@pytest.mark.asyncio
async def test_write_file_valid_workspace_path_still_reaches_governance(tmp_path):
    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)

    async def allow_governance(_context, _deps, *, event_callback=None):
        return None

    service.governance_runner = allow_governance
    registry = _ExplodingRegistry()

    async def write_handler(request):
        (context.workspace / "workspace").mkdir(exist_ok=True)
        (context.workspace / "workspace" / "ok.md").write_text("written", encoding="utf-8")
        return "✅ Written to workspace/ok.md"

    registry.try_execute = write_handler
    service.registry = registry

    await service.execute(
        "write_file",
        {"path": "workspace/ok.md", "content": "written"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )

    assert (context.workspace / "workspace" / "ok.md").read_text(encoding="utf-8") == "written"


_FS_WRITE_MODE_CASES = [
    ("write", {"content": "x"}),
    ("edit", {"old_string": "a", "new_string": "b"}),
    ("delete", {}),
    (None, {"content": "x"}),
]


def _fs_write_arguments(mode, extra: dict, path: str) -> dict:
    arguments = {"path": path, **extra}
    if mode is not None:
        arguments["mode"] = mode
    return arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,extra", _FS_WRITE_MODE_CASES)
async def test_fs_write_traversal_denied_before_governance(tmp_path, mode, extra):
    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)

    await _execute_and_assert_denied(
        service,
        context,
        "fs_write",
        _fs_write_arguments(mode, extra, "../escape.md"),
        reason_code="workspace_resource_path_escape",
    )
    assert not (tmp_path / "escape.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,extra", _FS_WRITE_MODE_CASES)
async def test_fs_write_cross_owner_denied_before_governance(tmp_path, mode, extra):
    context = _context(tmp_path)
    user_dir = context.workspace / "workspace"
    user_dir.mkdir(parents=True)
    (user_dir / "other-owner.md").write_text("owned", encoding="utf-8")
    context.workspace_authority_scope = WorkspaceAuthorityScope(
        agent_id=context.agent_id,
        user_id=context.user_id,
        root_session_id=None,
        allowed_paths=frozenset({"workspace/mine.md"}),
        operator_view=False,
        authority_source="test",
    )
    service = _service(context)

    await _execute_and_assert_denied(
        service,
        context,
        "fs_write",
        _fs_write_arguments(mode, extra, "workspace/other-owner.md"),
        reason_code="workspace_resource_forbidden",
    )
    assert (user_dir / "other-owner.md").read_text(encoding="utf-8") == "owned"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("office_document_create", {"path": "../escape.docx", "kind": "docx"}),
        (
            "office_document_create",
            {"path": "workspace/ok.docx", "kind": "docx", "template_path": "../escape-template.docx"},
        ),
        ("office_document_apply", {"path": "../escape.docx", "operations": [{}]}),
        (
            "office_document_apply",
            {"path": "workspace/ok.docx", "operations": [{}], "output_path": "../escape-out.docx"},
        ),
    ],
)
async def test_office_mutation_traversal_denied_before_governance(tmp_path, tool_name, arguments):
    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)

    await _execute_and_assert_denied(
        service,
        context,
        tool_name,
        arguments,
        reason_code="workspace_resource_path_escape",
    )
    assert not (tmp_path / "escape.docx").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("office_document_create", {"path": "workspace/other-owner.docx", "kind": "docx"}),
        ("office_document_apply", {"path": "workspace/other-owner.docx", "operations": [{}]}),
    ],
)
async def test_office_mutation_cross_owner_denied_before_governance(tmp_path, tool_name, arguments):
    context = _context(tmp_path)
    user_dir = context.workspace / "workspace"
    user_dir.mkdir(parents=True)
    (user_dir / "other-owner.docx").write_bytes(b"owned")
    context.workspace_authority_scope = WorkspaceAuthorityScope(
        agent_id=context.agent_id,
        user_id=context.user_id,
        root_session_id=None,
        allowed_paths=frozenset({"workspace/mine.docx"}),
        operator_view=False,
        authority_source="test",
    )
    service = _service(context)

    await _execute_and_assert_denied(
        service,
        context,
        tool_name,
        arguments,
        reason_code="workspace_resource_forbidden",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("fs_write", {"mode": "write", "path": "workspace/ok.md", "content": "x"}),
        ("office_document_create", {"path": "workspace/ok.docx", "kind": "docx"}),
        ("office_document_apply", {"path": "workspace/ok.docx", "operations": [{}]}),
        # Empty optional paths fall back in the final handlers and must not be
        # path-denied here.
        ("office_document_create", {"path": "workspace/ok.docx", "kind": "docx", "template_path": ""}),
        ("office_document_apply", {"path": "workspace/ok.docx", "operations": [{}], "output_path": ""}),
        # An output_path identical to the source needs no second authority
        # decision beyond the already-checked source path.
        (
            "office_document_apply",
            {"path": "workspace/ok.docx", "operations": [{}], "output_path": "workspace/ok.docx"},
        ),
    ],
)
async def test_valid_mutation_paths_reach_unavailable_governance(tmp_path, tool_name, arguments):
    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)

    with pytest.raises(AssertionError, match="governance runner reached"):
        await service.execute(
            tool_name,
            arguments,
            agent_id=context.agent_id,
            user_id=context.user_id,
            session_id=context.session_id,
        )


@pytest.mark.asyncio
async def test_governance_timeout_masking_control(tmp_path, monkeypatch):
    """A real governance timeout stays typed ``unavailable`` for a valid path,
    while the same outage must not mask a deterministic escape denial."""
    from app.tools import governance

    monkeypatch.setattr(governance, "_GOVERNANCE_TIMEOUT_SECONDS", 0.001)

    async def unavailable_pipeline(*_args, **_kwargs):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(governance, "_run_governance_inner", unavailable_pipeline)

    context = _context(tmp_path)
    context.workspace.mkdir(parents=True)
    service = _service(context)
    service.governance_runner = governance.run_tool_governance

    async def build_context(**kwargs):
        runtime_context = kwargs["runtime_context"]
        return governance.ToolGovernanceContext(
            agent_id=runtime_context.agent_id,
            user_id=runtime_context.user_id,
            tenant_id=runtime_context.tenant_id,
            tool_name=kwargs["tool_name"],
            arguments=kwargs["arguments"],
        )

    def build_dependencies():
        return governance.GovernanceDependencies(
            resolve_security_zone=lambda _agent_id: "standard",
            check_capability=lambda *_args: None,
            write_audit_event=lambda **_kwargs: None,
            request_approval=lambda **_kwargs: None,
        )

    service.governance_resolver = SimpleNamespace(
        build_context=build_context,
        build_dependencies=build_dependencies,
    )

    unavailable = await service.execute(
        "write_file",
        {"path": "workspace/ok.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )
    unavailable_payload = _error_payload(unavailable)
    assert unavailable_payload["error_class"] == "governance_dependency_unavailable"
    assert unavailable_payload.get("outcome") == "unavailable"

    denied = await service.execute(
        "write_file",
        {"path": "../escape.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id=context.session_id,
    )
    denied_payload = _error_payload(denied)
    assert denied_payload["error_class"] == "auth_or_permission"
    assert denied_payload["reason_code"] == "workspace_resource_path_escape"
    assert denied_payload.get("outcome") == "denied"
    assert not (tmp_path / "escape.md").exists()
