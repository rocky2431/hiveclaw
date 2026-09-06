"""J-01 regression: POST /api/enterprise/llm-models 201 must be committed before delivery.

FastAPI >= 0.118 tears down yield-scoped dependencies AFTER the response body is
sent, so ``get_db``'s implicit teardown commit runs after the client already
holds the 201. A create route that only flushes therefore reports a false
completion: the immediate follow-up request (POST /api/agents validating
``primary_model_id`` on a fresh session) cannot see the model and fails with
400 "primary_model_id 指向的模型不存在、未启用或不属于本公司" — exact CI run
34013473376, J-01 first attempt.

The test drives ``get_db`` exactly as FastAPI's DI does. "Handler returned" is
the instant the 201 body reaches the client (teardown has not run yet), so the
model must already be committed at that point.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.api import enterprise as enterprise_api
from app.api.agents import _validate_model_refs
from app.database import get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.schemas import LLMModelCreate


async def test_add_llm_model_committed_before_201_delivered(owner_sessionmaker, monkeypatch):
    token = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        tenant = Tenant(name=f"J01 commit {token}", slug=f"j01-commit-{token}")
        session.add(tenant)
        await session.flush()
        admin = User(
            username=f"j01-admin-{token}",
            email=f"j01-admin-{token}@test.invalid",
            password_hash="x",
            display_name="J01 admin",
            tenant_id=tenant.id,
            role="org_admin",
            is_active=True,
        )
        session.add(admin)
        await session.commit()
        tenant_id = tenant.id

    # get_db closes over the module-global async_session factory; bind it to
    # the integration container like the other real-PG tests do.
    monkeypatch.setattr("app.database.async_session", owner_sessionmaker)

    gen = get_db()
    db = await gen.__anext__()
    try:
        current_user = (
            (await db.execute(select(User).where(User.tenant_id == tenant_id, User.role == "org_admin")))
            .scalars()
            .one()
        )
        created = await enterprise_api.add_llm_model(
            LLMModelCreate(provider="openai", model="gpt-5.2", api_key="sk-j01-regression", label="J01 commit"),
            tenant_id=None,
            current_user=current_user,
            db=db,
        )

        # FastAPI 0.139 delivers the response body here, before get_db's
        # teardown commit. The independent session below is the production
        # follow-up request: it must already see a committed, enabled model —
        # this is exactly POST /api/agents' _validate_model_refs query.
        async with owner_sessionmaker() as independent:
            await _validate_model_refs(
                independent,
                tenant_id,
                primary_model_id=uuid.UUID(str(created.id)),
                fallback_model_id=None,
            )
    finally:
        # Complete the dependency lifecycle the way FastAPI does after send.
        try:
            await gen.__anext__()
            raise AssertionError("get_db yielded a second value")
        except StopAsyncIteration:
            pass
