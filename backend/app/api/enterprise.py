"""Enterprise management API routes: LLM pool, enterprise info, approvals, audit logs."""

from functools import partial
import logging
from pathlib import Path
import uuid

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_admin, get_current_user
from app.core.permissions import agent_owned_by_clause, is_scoped_business_admin
from app.core.tenant_scope import resolve_and_pin_tenant_scope
from app.config import get_settings
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog, EnterpriseInfo
from app.models.llm import LLMModel
from app.models.invitation_code import InvitationCode
from app.models.org import OrgDepartment, OrgMember
from app.models.system_settings import SystemSetting
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.schemas import (
    ApprovalAction,
    ApprovalRequestOut,
    EnterpriseInfoOut,
    EnterpriseInfoUpdate,
    LLMModelCreate,
    LLMModelOut,
    LLMModelUpdate,
)
from app.schemas.audit_schemas import AuditLogSummaryOut
from app.services.approval_service import approval_service
from app.services.enterprise_approval_visibility import enterprise_visible_approval_filter
from app.services.enterprise_sync import enterprise_sync_service
from app.services.llm_client import get_provider_manifest
from app.services.secrets_provider import get_secrets_provider

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/enterprise", tags=["enterprise"])
TENANT_SYSTEM_SETTING_KEYS = {"agent_permission_default", "feishu_org_sync"}
PLATFORM_SYSTEM_SETTING_KEYS = {"notification_bar", "platform"}


def _require_org_admin(current_user: User) -> None:
    """Company administrator gate (PDEC-013).

    Organization administrators and scoped platform administrators both
    manage company business inside the tenant their request resolved to;
    the tenant pinning happens per-route via ``resolve_and_pin_tenant_scope``.
    """
    if current_user.role not in ("org_admin", "platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization administrator access required",
        )


def _is_company_intro_setting(key: str) -> bool:
    return key == "company_intro" or key.startswith("company_intro_")


def _authorize_system_setting(current_user: User, key: str) -> None:
    allowed = (
        key in PLATFORM_SYSTEM_SETTING_KEYS
        if current_user.role == "platform_admin"
        else key in TENANT_SYSTEM_SETTING_KEYS or _is_company_intro_setting(key)
    )
    if current_user.role not in {"platform_admin", "org_admin"} or not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System setting is not available to this administrator role",
        )


def _project_system_setting_value(key: str, value: dict | None) -> dict:
    projected = dict(value or {})
    if key == "feishu_org_sync":
        projected["app_secret_configured"] = bool(projected.pop("app_secret", None))
    return projected


# ─── LLM Model Pool ────────────────────────────────────


@router.get("/llm-providers")
async def list_llm_providers(
    current_user: User = Depends(get_current_user),
):
    """List supported LLM providers and capabilities from registry."""
    return get_provider_manifest()


class LLMTestRequest(BaseModel):
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    model_id: str | None = None  # existing model ID to use stored API key
    temperature: float | None = None
    reasoning_mode: str | None = None
    reasoning_effort: str | None = None
    reasoning_budget_tokens: int | None = None
    reasoning_display: str | None = None
    preserve_reasoning: bool | None = None
    text_verbosity: str | None = None
    provider_options: dict | None = None


def _llm_test_probe_max_tokens(provider: str, model: str | None) -> int:
    from app.services.llm_client import uses_openai_responses_api

    return 1024 if uses_openai_responses_api(provider, model) else 16


async def _write_llm_test_audit(
    db: AsyncSession,
    *,
    data: LLMTestRequest,
    actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    max_tokens: int,
    phase: str,
    success: bool | None = None,
    latency_ms: int | None = None,
    error_type: str | None = None,
) -> None:
    from app.core.policy import write_audit_event

    try:
        resource_id = uuid.UUID(data.model_id) if data.model_id else None
    except ValueError:
        resource_id = None
    details: dict[str, object] = {
        "phase": phase,
        "provider": data.provider,
        "model": data.model,
        "max_tokens": max_tokens,
        "probe_id": str(request_id),
    }
    if success is not None:
        details.update(success=success, latency_ms=latency_ms or 0)
    if error_type:
        details["error_type"] = error_type
    await write_audit_event(
        db,
        event_type=f"llm_model.test_{phase}",
        severity="info" if success is not False else "warn",
        actor_type="user",
        actor_id=actor_id,
        tenant_id=tenant_id,
        action=f"test_llm_model_{phase}",
        resource_type="llm_model",
        resource_id=resource_id,
        details=details,
        request_id=request_id,
    )


@router.post("/llm-test")
async def test_llm_model(
    data: LLMTestRequest,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Test an LLM model configuration by making a simple API call."""
    import time
    from app.services.llm_client import create_llm_client, get_llm_model_identifier_error
    from app.services.llm_reasoning import build_reasoning_kwargs, resolve_temperature

    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    identifier_error = get_llm_model_identifier_error(data.provider, data.model)
    if identifier_error:
        return {"success": False, "latency_ms": 0, "error": identifier_error}

    # Resolve API key: use provided key, or look up from stored model
    api_key = data.api_key if data.api_key and not data.api_key.startswith("****") else None
    if not api_key and data.model_id:
        result = await db.execute(
            select(LLMModel).where(LLMModel.id == data.model_id, LLMModel.tenant_id == target_tenant_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            api_key = existing.api_key
    if not api_key:
        return {"success": False, "latency_ms": 0, "error": "API Key is required"}

    max_tokens = _llm_test_probe_max_tokens(data.provider, data.model)
    probe_id = uuid.uuid4()
    try:
        await _write_llm_test_audit(
            db,
            data=data,
            actor_id=current_user.id,
            tenant_id=target_tenant_id,
            request_id=probe_id,
            max_tokens=max_tokens,
            phase="started",
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error("Audit start write failed for llm_model test; provider call denied", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Security audit unavailable; model probe was not started",
        ) from exc

    start = time.monotonic()
    error_type = None
    try:
        client = create_llm_client(
            provider=data.provider,
            model=data.model,
            api_key=api_key,
            base_url=data.base_url or None,
        )
        # Simple test: ask model to say "ok"
        from app.services.llm_client import LLMMessage

        model_config = {
            "provider": data.provider,
            "model": data.model,
            "temperature": data.temperature,
            "reasoning_mode": data.reasoning_mode or "provider_default",
            "reasoning_effort": data.reasoning_effort,
            "reasoning_budget_tokens": data.reasoning_budget_tokens,
            "reasoning_display": data.reasoning_display,
            "preserve_reasoning": data.preserve_reasoning,
            "text_verbosity": data.text_verbosity,
            "provider_options": data.provider_options,
        }
        reasoning_kwargs = build_reasoning_kwargs(model_config, tools_enabled=False)
        response = await client.complete(
            messages=[LLMMessage(role="user", content="Say 'ok' and nothing else.")],
            temperature=resolve_temperature(model_config),
            max_tokens=max_tokens,
            **reasoning_kwargs,
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        reply = (response.content or "") if response else ""
        probe_result = {"success": True, "latency_ms": latency_ms, "reply": reply}
    except Exception as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        error_type = type(e).__name__
        probe_result = {"success": False, "latency_ms": latency_ms, "error": str(e)}

    try:
        await _write_llm_test_audit(
            db,
            data=data,
            actor_id=current_user.id,
            tenant_id=target_tenant_id,
            request_id=probe_id,
            max_tokens=max_tokens,
            phase="completed",
            success=probe_result["success"],
            latency_ms=latency_ms,
            error_type=error_type,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.error("Audit result write failed for llm_model test", exc_info=True)
        return {
            "success": False,
            "latency_ms": latency_ms,
            "error": "Model probe completed, but its audit result could not be persisted. Do not retry automatically.",
            "provider_success": probe_result["success"],
            "audit_status": "result_persistence_failed",
            "retryable": False,
        }
    return probe_result


@router.get("/llm-models")
async def list_llm_models(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List LLM models scoped to the selected tenant, with is_default flag."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)

    # Get default model ID from TenantSetting
    from app.models.tenant_setting import TenantSetting

    default_model_id = None
    ts_result = await db.execute(
        select(TenantSetting.value).where(
            TenantSetting.tenant_id == target_tenant_id,
            TenantSetting.key == "default_model_id",
        )
    )
    ts_val = ts_result.scalar_one_or_none()
    if isinstance(ts_val, dict):
        default_model_id = ts_val.get("model_id")

    query = select(LLMModel).where(LLMModel.tenant_id == target_tenant_id).order_by(LLMModel.created_at.desc())
    result = await db.execute(query)
    models = []
    first_model_id = None
    for m in result.scalars().all():
        out = LLMModelOut.model_validate(m)
        key = m.api_key_encrypted or ""
        out.api_key_masked = f"****{key[-4:]}" if len(key) > 4 else "****"
        if first_model_id is None:
            first_model_id = str(m.id)
        models.append(out)

    # If no default set, first created model is default
    effective_default = default_model_id or first_model_id
    for out in models:
        out.is_default = str(out.id) == effective_default

    return models


@router.put("/llm-models/default")
async def set_default_model(
    data: dict,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set the default LLM model for the tenant."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    model_id = data.get("model_id")
    if not model_id:
        raise HTTPException(status_code=400, detail="model_id required")

    # Verify model exists and belongs to tenant
    m = await db.execute(select(LLMModel).where(LLMModel.id == model_id, LLMModel.tenant_id == target_tenant_id))
    if not m.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Model not found")

    from app.models.tenant_setting import TenantSetting

    existing = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == target_tenant_id,
            TenantSetting.key == "default_model_id",
        )
    )
    ts = existing.scalar_one_or_none()
    if ts:
        ts.value = {"model_id": model_id}
    else:
        db.add(TenantSetting(tenant_id=target_tenant_id, key="default_model_id", value={"model_id": model_id}))
    await db.commit()
    return {"status": "ok", "default_model_id": model_id}


@router.post("/llm-models", response_model=LLMModelOut, status_code=status.HTTP_201_CREATED)
async def add_llm_model(
    data: LLMModelCreate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a new LLM model to the tenant's pool (admin)."""
    from app.services.llm_client import get_llm_model_identifier_error

    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    identifier_error = get_llm_model_identifier_error(data.provider, data.model)
    if identifier_error:
        raise HTTPException(status_code=422, detail=identifier_error)
    model = LLMModel(
        provider=data.provider,
        model=data.model,
        api_key_encrypted=get_secrets_provider().encrypt(data.api_key),
        base_url=data.base_url,
        label=data.label,
        max_tokens_per_day=data.max_tokens_per_day,
        enabled=data.enabled,
        supports_vision=data.supports_vision,
        max_output_tokens=data.max_output_tokens,
        max_input_tokens=data.max_input_tokens,
        temperature=data.temperature,
        reasoning_mode=data.reasoning_mode,
        reasoning_effort=data.reasoning_effort,
        reasoning_budget_tokens=data.reasoning_budget_tokens,
        reasoning_display=data.reasoning_display,
        preserve_reasoning=data.preserve_reasoning,
        text_verbosity=data.text_verbosity,
        provider_options=data.provider_options,
        tenant_id=target_tenant_id,
    )
    db.add(model)
    await db.flush()

    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="llm_model.created",
            severity="info",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=target_tenant_id,
            action="create_llm_model",
            resource_type="llm_model",
            resource_id=model.id,
            details={"provider": model.provider, "model": model.model, "label": model.label},
        )
    except Exception:
        logger.warning("Audit write failed for llm_model.created", exc_info=True)

    # FastAPI tears down yield dependencies after sending the response; commit
    # here so the 201 is durable before an immediate follow-up request.
    await db.commit()

    return LLMModelOut.model_validate(model)


@router.delete("/llm-models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_llm_model(
    model_id: uuid.UUID,
    force: bool = False,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove an LLM model from the pool (tenant-scoped)."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await db.execute(select(LLMModel).where(LLMModel.id == model_id, LLMModel.tenant_id == target_tenant_id))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    # Check if any agents reference this model
    from sqlalchemy import or_, update

    ref_result = await db.execute(
        select(Agent.name).where(
            Agent.tenant_id == target_tenant_id,
            or_(Agent.primary_model_id == model_id, Agent.fallback_model_id == model_id),
        )
    )
    agent_names = [row[0] for row in ref_result.all()]

    if agent_names and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"This model is used by {len(agent_names)} agent(s)",
                "agents": agent_names,
            },
        )

    # Nullify FK references in agents before deleting
    if agent_names:
        await db.execute(
            update(Agent)
            .where(Agent.tenant_id == target_tenant_id, Agent.primary_model_id == model_id)
            .values(primary_model_id=None)
        )
        await db.execute(
            update(Agent)
            .where(Agent.tenant_id == target_tenant_id, Agent.fallback_model_id == model_id)
            .values(fallback_model_id=None)
        )
    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="llm_model.deleted",
            severity="warn",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=target_tenant_id,
            action="delete_llm_model",
            resource_type="llm_model",
            resource_id=model.id,
            details={"provider": model.provider, "model": model.model, "force": force},
        )
    except Exception:
        logger.warning("Audit write failed for llm_model.deleted", exc_info=True)

    await db.delete(model)
    await db.commit()


@router.put("/llm-models/{model_id}", response_model=LLMModelOut)
async def update_llm_model(
    model_id: uuid.UUID,
    data: LLMModelUpdate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing LLM model in the pool (admin, tenant-scoped)."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await db.execute(select(LLMModel).where(LLMModel.id == model_id, LLMModel.tenant_id == target_tenant_id))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    try:
        from app.services.llm_client import get_llm_model_identifier_error

        effective_provider = data.provider if data.provider is not None else model.provider
        effective_model = data.model if data.model is not None else model.model
        identifier_error = get_llm_model_identifier_error(effective_provider, effective_model)
        if identifier_error:
            raise HTTPException(status_code=422, detail=identifier_error)

        if data.provider:
            model.provider = data.provider
        if data.model:
            model.model = data.model
        if data.label is not None:
            model.label = data.label
        if hasattr(data, "base_url") and data.base_url is not None:
            model.base_url = data.base_url
        if data.api_key and data.api_key.strip() and not data.api_key.startswith("****"):  # Skip masked values
            model.api_key_encrypted = get_secrets_provider().encrypt(data.api_key.strip())
        if data.max_tokens_per_day is not None:
            model.max_tokens_per_day = data.max_tokens_per_day
        if data.enabled is not None:
            model.enabled = data.enabled
        if hasattr(data, "supports_vision") and data.supports_vision is not None:
            model.supports_vision = data.supports_vision
        if hasattr(data, "max_output_tokens") and data.max_output_tokens is not None:
            model.max_output_tokens = data.max_output_tokens
        if hasattr(data, "max_input_tokens") and data.max_input_tokens is not None:
            model.max_input_tokens = data.max_input_tokens
        if "temperature" in data.model_fields_set:
            model.temperature = data.temperature
        if "reasoning_mode" in data.model_fields_set:
            model.reasoning_mode = data.reasoning_mode
        if "reasoning_effort" in data.model_fields_set:
            model.reasoning_effort = data.reasoning_effort
        if "reasoning_budget_tokens" in data.model_fields_set:
            model.reasoning_budget_tokens = data.reasoning_budget_tokens
        if "reasoning_display" in data.model_fields_set:
            model.reasoning_display = data.reasoning_display
        if "preserve_reasoning" in data.model_fields_set:
            model.preserve_reasoning = data.preserve_reasoning
        if "text_verbosity" in data.model_fields_set:
            model.text_verbosity = data.text_verbosity
        if "provider_options" in data.model_fields_set:
            model.provider_options = data.provider_options

        try:
            from app.core.policy import write_audit_event

            await write_audit_event(
                db,
                event_type="llm_model.updated",
                severity="info",
                actor_type="user",
                actor_id=current_user.id,
                tenant_id=target_tenant_id,
                action="update_llm_model",
                resource_type="llm_model",
                resource_id=model.id,
                details={"provider": model.provider, "model": model.model},
            )
        except Exception:
            logger.warning("Audit write failed for llm_model.updated", exc_info=True)

        await db.commit()
        await db.refresh(model)
        return LLMModelOut.model_validate(model)
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from sqlalchemy.exc import IntegrityError

        if isinstance(e, IntegrityError):
            raise HTTPException(status_code=409, detail="Conflict: model with these settings already exists")
        logger.error("Failed to update LLM model %s: %s", model_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to update model: {type(e).__name__}")


# ─── Enterprise Info ────────────────────────────────────


@router.get("/info", response_model=list[EnterpriseInfoOut])
async def list_enterprise_info(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all enterprise information entries."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await db.execute(
        select(EnterpriseInfo).where(EnterpriseInfo.tenant_id == target_tenant_id).order_by(EnterpriseInfo.info_type)
    )
    infos = [e for e in result.scalars().all() if getattr(e, "tenant_id", None) == target_tenant_id]
    return [EnterpriseInfoOut.model_validate(e) for e in infos]


@router.put("/info/{info_type}", response_model=EnterpriseInfoOut)
async def update_enterprise_info(
    info_type: str,
    data: EnterpriseInfoUpdate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update enterprise information. Triggers sync to agents."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    info = await enterprise_sync_service.update_enterprise_info(
        db, target_tenant_id, info_type, data.content, data.visible_roles, current_user.id
    )
    # Sync DB data → workspace files for agents to read (non-fatal).
    # Inline sync gives the editor immediate feedback; mark_tenant_dirty
    # additionally broadcasts to peer instances so their caches refresh.
    try:
        from app.services.workspace_sync import sync_company_profile
        from app.services.workspace_sync_dirty import mark_tenant_dirty

        await sync_company_profile(db, target_tenant_id)
        mark_tenant_dirty(target_tenant_id)
    except Exception as sync_err:
        logger.warning(f"Workspace sync failed (non-fatal): {sync_err}")
    await enterprise_sync_service.sync_to_all_agents(db, target_tenant_id)
    return EnterpriseInfoOut.model_validate(info)


# ─── Approvals ──────────────────────────────────────────


@router.get("/approvals", response_model=list[ApprovalRequestOut])
async def list_approvals(
    tenant_id: str | None = None,
    status_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List approval requests scoped to a tenant."""
    query = select(ApprovalRequest)
    # Scope by tenant: only show approvals for agents belonging to this tenant
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    tenant_agent_ids = select(Agent.id).where(Agent.tenant_id == target_tenant_id)
    query = query.where(ApprovalRequest.agent_id.in_(tenant_agent_ids))
    query = query.where(enterprise_visible_approval_filter(ApprovalRequest))
    # Scoped business administrators (PDEC-013) list every pending approval of
    # Agents in their selected/company tenant; everyone else is further
    # restricted to their own agents.
    if not is_scoped_business_admin(current_user, resource_tenant_id=target_tenant_id):
        query = query.where(
            ApprovalRequest.agent_id.in_(select(Agent.id).where(agent_owned_by_clause(current_user.id)))
        )
    if status_filter:
        query = query.where(ApprovalRequest.status == status_filter)
    query = query.order_by(ApprovalRequest.created_at.desc())

    result = await db.execute(query)
    approvals = result.scalars().all()

    # Batch-load agent names
    agent_ids_set = {a.agent_id for a in approvals}
    agent_names: dict[uuid.UUID, str] = {}
    if agent_ids_set:
        agents_r = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids_set)))
        agent_names = {row.id: row.name for row in agents_r.all()}

    out = []
    for a in approvals:
        d = ApprovalRequestOut.model_validate(a)
        d.agent_name = agent_names.get(a.agent_id)
        out.append(d)
    return out


@router.post("/approvals/{approval_id}/resolve", response_model=ApprovalRequestOut)
async def resolve_approval(
    approval_id: uuid.UUID,
    data: ApprovalAction,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject a pending approval request."""
    try:
        approval = await approval_service.resolve_approval(db, approval_id, current_user, data.action)
        return ApprovalRequestOut.model_validate(approval)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Audit Logs ─────────────────────────────────────────


def _require_platform_admin(current_user: User) -> None:
    if current_user.role != "platform_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        )


@router.get("/platform-security-audit")
async def list_platform_security_audit_events(
    event_type: str | None = Query(default=None, max_length=100),
    severity: str | None = Query(default=None, max_length=20),
    actor_id: uuid.UUID | None = None,
    request_id: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_admin),
):
    """Query the tenantless, tamper-evident operator security audit plane."""
    _require_platform_admin(current_user)
    from app.services.platform_security_audit import query_platform_security_audit_events

    return await query_platform_security_audit_events(
        event_type=event_type,
        severity=severity,
        actor_id=actor_id,
        request_id=request_id,
        limit=limit,
        offset=offset,
    )


@router.get("/platform-security-audit/verify")
async def verify_platform_security_audit_chain(
    current_user: User = Depends(get_current_admin),
):
    """Verify the complete operator security chain and its immutable legacy anchor."""
    _require_platform_admin(current_user)
    from app.services.platform_security_audit import verify_persisted_platform_security_audit_chain

    return await verify_persisted_platform_security_audit_chain()


@router.get("/audit-logs", response_model=list[AuditLogSummaryOut])
async def list_audit_logs(
    agent_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
    limit: int = 50,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List audit logs scoped to a tenant (admin only)."""
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    # Scope by tenant: only show logs for agents belonging to this tenant
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    tenant_agent_ids = select(Agent.id).where(Agent.tenant_id == target_tenant_id)
    query = query.where(AuditLog.agent_id.in_(tenant_agent_ids))
    if agent_id:
        query = query.where(AuditLog.agent_id == agent_id)
    result = await db.execute(query)
    return [AuditLogSummaryOut.model_validate(log) for log in result.scalars().all()]


# ─── Security Audit (SecurityAuditEvent table) ─────────


@router.get("/audit")
async def query_audit_events(
    tenant_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    actor_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 50,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Unified audit query over SecurityAuditEvent table (admin only)."""
    from datetime import datetime as dt

    from app.schemas.audit_schemas import AuditEventOut, AuditQueryParams
    from app.services.audit_query_service import query_events

    params = AuditQueryParams(
        event_type=event_type,
        severity=severity,
        actor_id=actor_id,
        resource_type=resource_type,
        resource_id=resource_id,
        search=search,
        date_from=dt.fromisoformat(date_from) if date_from else None,
        date_to=dt.fromisoformat(date_to) if date_to else None,
        page=page,
        page_size=page_size,
    )

    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    events, total = await query_events(db, target_tenant_id, params)
    return {
        "items": [AuditEventOut.model_validate(e) for e in events],
        "total": total,
        "page": params.page,
        "page_size": params.page_size,
    }


@router.get("/audit/export")
async def export_audit_csv(
    tenant_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    actor_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Export filtered audit events as CSV (admin only)."""
    from datetime import datetime as dt

    from fastapi.responses import StreamingResponse

    from app.schemas.audit_schemas import AuditQueryParams
    from app.services.audit_query_service import export_csv

    params = AuditQueryParams(
        event_type=event_type,
        severity=severity,
        actor_id=actor_id,
        resource_type=resource_type,
        resource_id=resource_id,
        search=search,
        date_from=dt.fromisoformat(date_from) if date_from else None,
        date_to=dt.fromisoformat(date_to) if date_to else None,
    )

    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    csv_data = await export_csv(db, target_tenant_id, params)
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_events.csv"},
    )


@router.get("/audit/{event_id}/chain")
async def verify_audit_chain(
    event_id: uuid.UUID,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Verify hash-chain integrity for a single audit event (admin only, tenant-scoped)."""
    from app.services.audit_query_service import verify_chain

    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    return await verify_chain(db, event_id, target_tenant_id)


# ─── Dashboard Stats ────────────────────────────────────


@router.get("/stats")
async def get_enterprise_stats(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get enterprise dashboard statistics, optionally scoped to a tenant."""
    # Determine which tenant to filter by
    tid = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)

    total_agents = await db.execute(select(func.count(Agent.id)).where(Agent.tenant_id == tid))
    running_agents = await db.execute(
        select(func.count(Agent.id)).where(Agent.tenant_id == tid, Agent.status == "running")
    )
    total_users = await db.execute(select(func.count(User.id)).where(User.tenant_id == tid, User.is_active))
    tenant_agent_ids = select(Agent.id).where(Agent.tenant_id == tid)
    pending_approvals = await db.execute(
        select(func.count(ApprovalRequest.id)).where(
            ApprovalRequest.status == "pending",
            ApprovalRequest.agent_id.in_(tenant_agent_ids),
            enterprise_visible_approval_filter(ApprovalRequest),
        )
    )

    return {
        "total_agents": total_agents.scalar() or 0,
        "running_agents": running_agents.scalar() or 0,
        "total_users": total_users.scalar() or 0,
        "pending_approvals": pending_approvals.scalar() or 0,
    }


# ─── Tenant Quota Settings ──────────────────────────────


class TenantQuotaUpdate(BaseModel):
    default_tokens_per_day: int | None = None
    default_tokens_per_month: int | None = None
    default_max_triggers: int | None = None
    min_poll_interval_floor: int | None = None
    max_webhook_rate_ceiling: int | None = None


@router.get("/tenant-quotas")
async def get_tenant_quotas(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tenant quota defaults."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await db.execute(select(Tenant).where(Tenant.id == target_tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        return {}
    return {
        "default_tokens_per_day": tenant.default_tokens_per_day,
        "default_tokens_per_month": tenant.default_tokens_per_month,
        "default_max_triggers": tenant.default_max_triggers,
        "min_poll_interval_floor": tenant.min_poll_interval_floor,
        "max_webhook_rate_ceiling": tenant.max_webhook_rate_ceiling,
    }


@router.patch("/tenant-quotas")
async def update_tenant_quotas(
    data: TenantQuotaUpdate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update tenant quota defaults (admin only)."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await db.execute(select(Tenant).where(Tenant.id == target_tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if data.default_tokens_per_day is not None:
        tenant.default_tokens_per_day = data.default_tokens_per_day
    if data.default_tokens_per_month is not None:
        tenant.default_tokens_per_month = data.default_tokens_per_month

    # Handle trigger limit fields
    if data.default_max_triggers is not None:
        tenant.default_max_triggers = data.default_max_triggers
    if data.min_poll_interval_floor is not None:
        tenant.min_poll_interval_floor = data.min_poll_interval_floor
    if data.max_webhook_rate_ceiling is not None:
        tenant.max_webhook_rate_ceiling = data.max_webhook_rate_ceiling

    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="quotas.updated",
            severity="info",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=target_tenant_id,
            action="update_tenant_quotas",
            resource_type="tenant",
            resource_id=target_tenant_id,
            details=data.model_dump(exclude_unset=True),
        )
    except Exception:
        logger.warning("Audit write failed for quotas.updated", exc_info=True)

    await db.commit()
    return {
        "message": "Tenant quotas updated",
        "heartbeat_agents_adjusted": 0,
    }


# ─── System Settings ───────────────────────────────────


class SettingUpdate(BaseModel):
    value: dict


# ─── OIDC Configuration ──────────────────────────────


class OIDCConfigUpdate(BaseModel):
    issuer_url: str
    client_id: str
    client_secret: str
    scopes: str = "openid profile email"
    auto_provision: bool = True
    display_name: str = "SSO"


@router.get("/oidc-config")
async def get_oidc_config(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get OIDC SSO configuration for the current tenant (admin only)."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)

    from app.models.tenant_setting import TenantSetting

    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == target_tenant_id,
            TenantSetting.key == "oidc_config",
        )
    )
    setting = result.scalar_one_or_none()
    if not setting or not setting.value:
        return {"configured": False}

    cfg = setting.value
    return {
        "configured": bool(cfg.get("issuer_url") and cfg.get("client_id")),
        "issuer_url": cfg.get("issuer_url", ""),
        "client_id": cfg.get("client_id", ""),
        "client_secret_set": bool(cfg.get("client_secret")),
        "scopes": cfg.get("scopes", "openid profile email"),
        "auto_provision": cfg.get("auto_provision", True),
        "display_name": cfg.get("display_name", "SSO"),
    }


@router.put("/oidc-config")
async def update_oidc_config(
    data: OIDCConfigUpdate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set or update OIDC SSO configuration for the current tenant (admin only)."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)

    # Validate issuer URL by attempting discovery
    from app.services.oidc_service import discover_oidc

    try:
        metadata = await discover_oidc(data.issuer_url)
        if "authorization_endpoint" not in metadata:
            raise HTTPException(status_code=400, detail="Invalid OIDC issuer: missing authorization_endpoint")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"Cannot reach OIDC issuer: {e}")

    from app.models.tenant_setting import TenantSetting

    result = await db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == target_tenant_id,
            TenantSetting.key == "oidc_config",
        )
    )
    setting = result.scalar_one_or_none()

    config_value = {
        "issuer_url": data.issuer_url,
        "client_id": data.client_id,
        "client_secret": data.client_secret,
        "scopes": data.scopes,
        "auto_provision": data.auto_provision,
        "display_name": data.display_name,
    }

    if setting:
        # If client_secret looks masked, keep existing
        if data.client_secret.startswith("****") and setting.value.get("client_secret"):
            config_value["client_secret"] = setting.value["client_secret"]
        setting.value = config_value
    else:
        db.add(
            TenantSetting(
                tenant_id=target_tenant_id,
                key="oidc_config",
                value=config_value,
            )
        )

    try:
        from app.core.policy import write_audit_event

        await write_audit_event(
            db,
            event_type="oidc.config_updated",
            severity="warn",
            actor_type="user",
            actor_id=current_user.id,
            tenant_id=target_tenant_id,
            action="update_oidc_config",
            details={"issuer_url": data.issuer_url, "client_id": data.client_id},
        )
    except Exception:
        logger.warning("Audit write failed for oidc.config_updated", exc_info=True)

    await db.commit()
    return {"status": "ok", "issuer_url": data.issuer_url}


# ─── System Settings ───────────────────────────────────


@router.get("/system-settings/notification_bar/public")
async def get_notification_bar_public(
    db: AsyncSession = Depends(get_db),
):
    """Public (no auth) endpoint to read the notification bar config."""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == "notification_bar"))
    setting = result.scalar_one_or_none()
    if not setting or not setting.value:
        return {"enabled": False, "text": ""}
    return {
        "enabled": setting.value.get("enabled", False),
        "text": setting.value.get("text", ""),
    }


@router.get("/system-settings/{key}")
async def get_system_setting(
    key: str,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get a system setting by key (admin only)."""
    _authorize_system_setting(current_user, key)
    if key in TENANT_SYSTEM_SETTING_KEYS:
        from app.models.tenant_setting import TenantSetting

        target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
        result = await db.execute(
            select(TenantSetting).where(
                TenantSetting.tenant_id == target_tenant_id,
                TenantSetting.key == key,
            )
        )
        setting = result.scalar_one_or_none()
        if not setting:
            return {"key": key, "value": _project_system_setting_value(key, {})}
        return {
            "key": setting.key,
            "value": _project_system_setting_value(key, setting.value),
            "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
        }

    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    if not setting:
        return {"key": key, "value": {}}
    return {
        "key": setting.key,
        "value": _project_system_setting_value(key, setting.value),
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


@router.put("/system-settings/{key}")
async def update_system_setting(
    key: str,
    data: SettingUpdate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update a system setting."""
    _authorize_system_setting(current_user, key)
    if key in TENANT_SYSTEM_SETTING_KEYS:
        from app.models.tenant_setting import TenantSetting

        if key == "agent_permission_default":
            from app.runtime.ccplus_contracts import tenant_permission_default_from_value

            requested_mode = data.value.get("mode") if isinstance(data.value, dict) else None
            if requested_mode not in {"default", "auto"}:
                raise HTTPException(
                    status_code=422,
                    detail="Tenant permission default must be default or auto; break-glass is session-scoped only",
                )
            data.value = {"mode": tenant_permission_default_from_value(data.value)}

        target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
        result = await db.execute(
            select(TenantSetting).where(
                TenantSetting.tenant_id == target_tenant_id,
                TenantSetting.key == key,
            )
        )
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = data.value
        else:
            setting = TenantSetting(tenant_id=target_tenant_id, key=key, value=data.value)
            db.add(setting)
        await db.commit()
        return {"key": setting.key, "value": _project_system_setting_value(key, setting.value)}

    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = data.value
    else:
        setting = SystemSetting(key=key, value=data.value)
        db.add(setting)
    await db.commit()
    return {"key": setting.key, "value": _project_system_setting_value(key, setting.value)}


# ─── Org Structure ──────────────────────────────────────


@router.get("/org/departments")
async def list_org_departments(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all departments, optionally filtered by tenant."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    query = select(OrgDepartment).where(OrgDepartment.tenant_id == target_tenant_id)
    result = await db.execute(query.order_by(OrgDepartment.name))
    depts = [d for d in result.scalars().all() if getattr(d, "tenant_id", None) == target_tenant_id]
    return [
        {
            "id": str(d.id),
            "feishu_id": d.feishu_id,
            "name": d.name,
            "parent_id": str(d.parent_id) if d.parent_id else None,
            "path": d.path,
            "member_count": d.member_count,
        }
        for d in depts
    ]


@router.get("/org/members")
async def list_org_members(
    department_id: str | None = None,
    search: str | None = None,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List org members, optionally filtered by department, search, or tenant."""
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    query = select(OrgMember).where(
        OrgMember.status == "active",
        OrgMember.tenant_id == target_tenant_id,
    )
    if department_id:
        query = query.where(OrgMember.department_id == uuid.UUID(department_id))
    if search:
        query = query.where(OrgMember.name.ilike(f"%{search}%"))
    query = query.order_by(OrgMember.name).limit(100)
    result = await db.execute(query)
    members = [m for m in result.scalars().all() if getattr(m, "tenant_id", None) == target_tenant_id]
    return [
        {
            "id": str(m.id),
            "name": m.name,
            "email": m.email,
            "title": m.title,
            "department_path": m.department_path,
            "avatar_url": m.avatar_url,
        }
        for m in members
    ]


@router.post("/org/sync")
async def trigger_org_sync(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger org structure sync from Feishu."""
    _require_org_admin(current_user)
    from app.services.org_sync_service import org_sync_service

    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    result = await org_sync_service.full_sync(target_tenant_id)

    # Sync org structure to workspace files + broadcast dirty mark to peers
    from app.services.workspace_sync import sync_org_structure
    from app.services.workspace_sync_dirty import mark_tenant_dirty
    from app.database import tenant_scoped_session

    async with tenant_scoped_session(target_tenant_id) as db:
        await sync_org_structure(db, target_tenant_id)
    mark_tenant_dirty(target_tenant_id)

    return result


# ─── Invitation Codes ───────────────────────────────────


class InvitationCodeCreate(BaseModel):
    count: int = Field(default=1, ge=1, le=100)
    max_uses: int = Field(default=1, ge=1)

    model_config = {"extra": "forbid"}


def _require_tenant_admin(current_user: User) -> None:
    """Check that the user is an organization administrator with a tenant."""
    _require_org_admin(current_user)
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No company assigned")


def _legacy_company_files_dir(tenant_id: uuid.UUID) -> Path:
    return Path(settings.AGENT_DATA_DIR) / f"enterprise_info_{tenant_id}"


@router.get("/legacy-company-files/status")
async def get_legacy_company_files_status(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Report isolated files left by the retired Company KB file tree."""

    from app.services.legacy_company_files import (
        LegacyCompanyFilesChangedError,
        LegacyCompanyFilesUnavailableError,
        scan_legacy_company_files,
    )

    _require_tenant_admin(current_user)
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    try:
        snapshot = await anyio.to_thread.run_sync(
            scan_legacy_company_files,
            _legacy_company_files_dir(target_tenant_id),
        )
    except LegacyCompanyFilesChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail="Retired shared files changed while they were being checked; retry the check",
        ) from exc
    except LegacyCompanyFilesUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="Retired shared files are temporarily unavailable; retry later",
        ) from exc
    return {
        "available": snapshot.file_count > 0,
        "file_count": snapshot.file_count,
        "total_bytes": snapshot.total_bytes,
        "excluded_symlink_count": snapshot.excluded_symlink_count,
        "read_only": True,
        "retired": True,
        "surface_kind": "legacy_company_files_quarantine",
        "company_kb_available": False,
        "agent_consumable": False,
    }


@router.get("/legacy-company-files/export")
async def export_legacy_company_files(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download an immutable snapshot; this endpoint never edits legacy files."""

    from app.services.legacy_company_files import (
        LegacyCompanyFilesChangedError,
        LegacyCompanyFilesUnavailableError,
        build_legacy_company_files_export,
    )

    _require_tenant_admin(current_user)
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    try:
        archive = await anyio.to_thread.run_sync(
            partial(
                build_legacy_company_files_export,
                _legacy_company_files_dir(target_tenant_id),
                tenant_id=str(target_tenant_id),
            )
        )
    except LegacyCompanyFilesChangedError as exc:
        raise HTTPException(status_code=409, detail="Legacy files changed during export; retry the export") from exc
    except LegacyCompanyFilesUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="Retired shared files are temporarily unavailable; retry the export later",
        ) from exc
    if archive.snapshot.file_count == 0:
        archive.stream.close()
        raise HTTPException(status_code=404, detail="No retired company files are available for export")

    try:
        db.add(
            AuditLog(
                tenant_id=target_tenant_id,
                user_id=current_user.id,
                action="legacy_company_files_exported",
                details={
                    "schema": "hive.legacy_company_files_export.v1",
                    "file_count": archive.snapshot.file_count,
                    "total_bytes": archive.snapshot.total_bytes,
                    "excluded_symlink_count": archive.snapshot.excluded_symlink_count,
                    "read_only": True,
                },
            )
        )
        await db.commit()
    except BaseException:
        archive.stream.close()
        raise

    async def stream_archive():
        try:
            while chunk := await anyio.to_thread.run_sync(archive.stream.read, 1024 * 1024):
                yield chunk
        finally:
            archive.stream.close()

    return StreamingResponse(
        stream_archive(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{archive.filename}"',
            "Content-Length": str(archive.size_bytes),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/invitation-codes")
async def create_invitation_codes(
    data: InvitationCodeCreate,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Batch-create invitation codes for the current user's company."""
    _require_tenant_admin(current_user)
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    import secrets
    import string

    codes_created = []
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(data.count):
        code_str = "".join(secrets.choice(alphabet) for _ in range(8))
        code = InvitationCode(
            code=code_str,
            tenant_id=target_tenant_id,
            max_uses=data.max_uses,
            created_by=current_user.id,
            granted_role="member",
        )
        db.add(code)
        codes_created.append(code_str)

    await db.commit()
    return {"created": len(codes_created), "codes": codes_created}


@router.get("/invitation-codes")
async def list_invitation_codes(
    page: int = 1,
    page_size: int = 20,
    search: str = "",
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List invitation codes for the current user's company."""
    _require_tenant_admin(current_user)
    from sqlalchemy import func as sqla_func

    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)

    base_filter = (
        InvitationCode.tenant_id == target_tenant_id,
        InvitationCode.granted_role == "member",
    )
    stmt = select(InvitationCode).where(*base_filter)
    count_stmt = select(sqla_func.count()).select_from(InvitationCode).where(*base_filter)

    if search:
        stmt = stmt.where(InvitationCode.code.ilike(f"%{search}%"))
        count_stmt = count_stmt.where(InvitationCode.code.ilike(f"%{search}%"))

    total_result = await db.execute(count_stmt)
    total = total_result.scalar() or 0

    offset = (max(page, 1) - 1) * page_size
    result = await db.execute(stmt.order_by(InvitationCode.created_at.desc()).offset(offset).limit(page_size))
    codes = result.scalars().all()
    return {
        "items": [
            {
                "id": str(c.id),
                "code": c.code,
                "max_uses": c.max_uses,
                "used_count": c.used_count,
                "is_active": c.is_active,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in codes
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/invitation-codes/export")
async def export_invitation_codes_csv(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export invitation codes for the current user's company as CSV."""
    _require_tenant_admin(current_user)
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    import csv
    import io
    from fastapi.responses import StreamingResponse

    result = await db.execute(
        select(InvitationCode)
        .where(
            InvitationCode.tenant_id == target_tenant_id,
            InvitationCode.granted_role == "member",
        )
        .order_by(InvitationCode.created_at.asc())
    )
    codes = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Code", "Max Uses", "Used Count", "Active", "Created At"])
    for c in codes:
        writer.writerow(
            [
                c.code,
                c.max_uses,
                c.used_count,
                "Yes" if c.is_active else "No",
                c.created_at.strftime("%Y-%m-%d %H:%M:%S") if c.created_at else "",
            ]
        )

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invitation_codes.csv"},
    )


@router.delete("/invitation-codes/{code_id}")
async def deactivate_invitation_code(
    code_id: str,
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate an invitation code (must belong to current user's company)."""
    _require_tenant_admin(current_user)
    target_tenant_id = await resolve_and_pin_tenant_scope(db, current_user, tenant_id)
    import uuid as _uuid

    result = await db.execute(
        select(InvitationCode).where(
            InvitationCode.id == _uuid.UUID(code_id),
            InvitationCode.tenant_id == target_tenant_id,
            InvitationCode.granted_role == "member",
        )
    )
    code = result.scalar_one_or_none()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found")
    code.is_active = False
    await db.commit()
    return {"status": "deactivated"}
