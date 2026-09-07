from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "hr_runtime_authority_0715.py"
PARENT_REVISION = "storage_blob_lifecycle_0715"
IMMUTABILITY_TRIGGER = "trg_hr_creation_blueprint_immutable"


def _alembic(database_url: str, command: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"


def _blueprint_hash(blueprint: dict) -> str:
    encoded = json.dumps(blueprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"bp_{hashlib.sha256(encoded).hexdigest()[:24]}"


def test_hr_runtime_authority_migration_contract() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "hr_runtime_authority_0715"' in source
    assert f'down_revision = "{PARENT_REVISION}"' in source
    assert "blueprint_payload_hash" in source
    assert "migration.hr_runtime_authority_quarantined" in source
    assert IMMUTABILITY_TRIGGER in source
    assert "IS DISTINCT FROM" in source
    assert "active_legacy_worker_fenced" in source
    assert "claim_version = claim_version + 1" in source
    assert "FOR UPDATE OF task" in source
    assert "'failed', 'completed'" in source
    assert "secure downgrade" in source.lower()


def test_fresh_bootstrap_wires_the_same_hr_blueprint_immutability_guard() -> None:
    bootstrap = (MIGRATION.parents[2] / "app" / "db_bootstrap.py").read_text(encoding="utf-8")

    assert "def apply_hr_creation_blueprint_immutability" in bootstrap
    assert "CREATE OR REPLACE FUNCTION enforce_hr_creation_blueprint_immutability()" in bootstrap
    assert IMMUTABILITY_TRIGGER in bootstrap
    assert bootstrap.count("apply_hr_creation_blueprint_immutability(connection)") == 2


@pytest.mark.asyncio
async def test_fresh_bootstrap_installs_hr_blueprint_immutability_guard(migrated_pg_url: str) -> None:
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            trigger = (
                await connection.execute(
                    text(
                        "SELECT c.relname, t.tgenabled, pg_get_triggerdef(t.oid) AS definition "
                        "FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal AND t.tgname = :name"
                    ),
                    {"name": IMMUTABILITY_TRIGGER},
                )
            ).one_or_none()

        assert trigger is not None
        assert trigger.relname == "hr_creation_drafts"
        assert trigger.tgenabled == b"O"
        assert "BEFORE UPDATE" in trigger.definition
        assert "blueprint_version" in trigger.definition
        assert "blueprint_hash" in trigger.definition
        assert "blueprint_json" in trigger.definition
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_migration_backfills_valid_snapshot_and_quarantines_invalid_authority(pg_container) -> None:
    from app.models.agent import Agent
    from app.models.hr_creation import HrCreationDraft
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.hr_provisioning_runtime import _runtime_authority_issues
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import (
        insert_chat_session_at_schema_revision,
        insert_runtime_task_at_schema_revision,
    )

    database_name = f"hr_runtime_authority_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    # Empty databases take the create_all + stamp bootstrap path. Rewind only
    # the receipt so the second upgrade executes this data migration against
    # representative pre-release rows; the secure trigger is compatible with
    # the parent application and intentionally remains installed.
    _alembic(database_url, "upgrade", "head")
    _alembic(database_url, "downgrade", PARENT_REVISION)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    valid_draft_id = uuid.uuid4()
    valid_task_id = uuid.uuid4()
    invalid_draft_id = uuid.uuid4()
    invalid_task_id = uuid.uuid4()
    completed_draft_id = uuid.uuid4()
    completed_task_id = uuid.uuid4()
    confirmed_at = datetime.now(timezone.utc)
    blueprint = {"name": "Migration Employee", "role_description": "Canonical authority."}
    blueprint_hash = _blueprint_hash(blueprint)

    engine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions.begin() as session:
            session.add(Tenant(id=tenant_id, name="HR authority", slug=f"hr-authority-{tenant_id.hex[:8]}"))
            session.add(
                User(
                    id=user_id,
                    username=f"hr-authority-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@hr-authority.test",
                    password_hash="x",
                    display_name="HR Authority Owner",
                    tenant_id=tenant_id,
                )
            )
            await session.flush()
            session.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name="__system_hr__",
                    role_description="System HR",
                    creator_id=user_id,
                    sponsor_user_id=user_id,
                    owner_user_id=user_id,
                    agent_class="internal_system",
                    status="running",
                )
            )
            await session.flush()
            await insert_chat_session_at_schema_revision(
                session,
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            session.add_all(
                [
                    HrCreationDraft(
                        id=valid_draft_id,
                        tenant_id=tenant_id,
                        hr_agent_id=agent_id,
                        session_id=session_id,
                        requested_by_user_id=user_id,
                        status="confirmed",
                        blueprint_version=2,
                        blueprint_hash=blueprint_hash,
                        blueprint_json=blueprint,
                        preview_json={"status": "preview"},
                        confirmed_by_user_id=user_id,
                        confirmed_at=confirmed_at,
                    ),
                    HrCreationDraft(
                        id=invalid_draft_id,
                        tenant_id=tenant_id,
                        hr_agent_id=agent_id,
                        session_id=session_id,
                        requested_by_user_id=user_id,
                        status="confirmed",
                        blueprint_version=1,
                        blueprint_hash=blueprint_hash,
                        blueprint_json=blueprint,
                        preview_json={"status": "preview"},
                        confirmed_by_user_id=user_id,
                        confirmed_at=None,
                        claim_token=uuid.uuid4(),
                        claim_version=4,
                        claim_heartbeat_at=confirmed_at,
                        claim_expires_at=confirmed_at,
                    ),
                    HrCreationDraft(
                        id=completed_draft_id,
                        tenant_id=tenant_id,
                        hr_agent_id=agent_id,
                        session_id=session_id,
                        requested_by_user_id=user_id,
                        status="completed",
                        blueprint_version=1,
                        blueprint_hash=blueprint_hash,
                        blueprint_json=blueprint,
                        preview_json={"status": "preview"},
                        confirmed_by_user_id=user_id,
                        confirmed_at=confirmed_at,
                    ),
                ]
            )
            await session.flush()
            for task_id, draft_id, version, task_status in (
                (valid_task_id, valid_draft_id, 2, "pending"),
                (invalid_task_id, invalid_draft_id, 1, "running"),
                (completed_task_id, completed_draft_id, 1, "completed"),
            ):
                await insert_runtime_task_at_schema_revision(
                    session,
                    id=task_id,
                    task_type="hr_provisioning",
                    parent_agent_id=agent_id,
                    tenant_id=tenant_id,
                    status=task_status,
                    scheduled_at=confirmed_at,
                    priority=24,
                    prompt="Provision the authenticated canonical HR blueprint.",
                    trace_id=f"hr-provisioning:{draft_id}",
                    parent_session_id=str(session_id),
                    root_user_id=user_id,
                    root_session_id=str(session_id),
                    delegation_chain_json=[f"agent:{agent_id}"],
                    depth=1,
                    root_idempotency_key=f"hr-provisioning:{draft_id}-v{version}",
                    config_snapshot_hash="legacy-config-hash".ljust(64, "0"),
                    policy_snapshot_hash="legacy-policy-hash".ljust(64, "0"),
                    metadata_json={
                        "schema": "hr_provisioning_job.v1",
                        "draft_id": str(draft_id),
                        "blueprint_version": version,
                        "blueprint_hash": blueprint_hash,
                        "phase": "queued",
                    },
                    claimed_by="legacy-running-worker" if task_status == "running" else None,
                    claim_version=7 if task_status == "running" else 0,
                    claim_expires_at=confirmed_at if task_status == "running" else None,
                    completed_at=confirmed_at if task_status == "completed" else None,
                )
            await session.flush()
            valid_draft = await session.get(HrCreationDraft, valid_draft_id)
            invalid_draft = await session.get(HrCreationDraft, invalid_draft_id)
            completed_draft = await session.get(HrCreationDraft, completed_draft_id)
            assert valid_draft is not None and invalid_draft is not None and completed_draft is not None
            valid_draft.provisioning_task_id = valid_task_id
            invalid_draft.provisioning_task_id = invalid_task_id
            completed_draft.provisioning_task_id = completed_task_id
    finally:
        await engine.dispose()

    verify_seed_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with verify_seed_engine.connect() as connection:
            seeded_links = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT id, provisioning_task_id FROM hr_creation_drafts "
                            "WHERE id IN (:valid_id, :invalid_id, :completed_id)"
                        ),
                        {
                            "valid_id": valid_draft_id,
                            "invalid_id": invalid_draft_id,
                            "completed_id": completed_draft_id,
                        },
                    )
                ).all()
            )
        assert seeded_links == {
            valid_draft_id: valid_task_id,
            invalid_draft_id: invalid_task_id,
            completed_draft_id: completed_task_id,
        }
    finally:
        await verify_seed_engine.dispose()

    _alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            valid_draft = await session.get(HrCreationDraft, valid_draft_id)
            valid_task = await session.get(RuntimeTask, valid_task_id)
            invalid_task = await session.get(RuntimeTask, invalid_task_id)
            completed_task = await session.get(RuntimeTask, completed_task_id)
            invalid_draft = await session.get(HrCreationDraft, invalid_draft_id)
            assert (
                valid_draft is not None
                and valid_task is not None
                and invalid_task is not None
                and completed_task is not None
                and invalid_draft is not None
            )
            version = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert version == "session_v2_delete_fk_indexes_0907"
            valid_issues = _runtime_authority_issues(valid_task, valid_draft)
            assert valid_issues == [], {
                "issues": valid_issues,
                "status": valid_task.status,
                "metadata": valid_task.metadata_json,
                "config_snapshot_hash": valid_task.config_snapshot_hash,
                "policy_snapshot_hash": valid_task.policy_snapshot_hash,
            }
            assert valid_task.metadata_json["blueprint_payload_hash"]
            assert valid_task.config_snapshot_hash != "legacy-config-hash".ljust(64, "0")
            assert valid_task.policy_snapshot_hash != "legacy-policy-hash".ljust(64, "0")
            assert invalid_task.status == "needs_reconciliation"
            assert invalid_task.claim_version == 8
            assert invalid_task.claimed_by is None
            assert invalid_task.claim_expires_at is None
            assert "active_legacy_worker_fenced" in invalid_task.metadata_json["authority_issues"]
            assert invalid_draft.claim_version == 5
            assert invalid_draft.claim_token is None
            assert invalid_draft.claim_expires_at is None
            assert completed_task.status == "completed"
            assert completed_task.metadata_json["authority_terminal_preserved_by"] == "hr_runtime_authority_0715"
            audit_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE action = 'migration.hr_runtime_authority_quarantined' "
                        "AND details ->> 'runtime_task_id' = :task_id"
                    ),
                    {"task_id": str(invalid_task_id)},
                )
            ).scalar_one()
            assert audit_count == 1
            completed_audit_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE action = 'migration.hr_runtime_authority_quarantined' "
                        "AND details ->> 'runtime_task_id' = :task_id"
                    ),
                    {"task_id": str(completed_task_id)},
                )
            ).scalar_one()
            assert completed_audit_count == 0

        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "UPDATE hr_creation_drafts SET blueprint_json = CAST(:blueprint AS jsonb) WHERE id = :draft_id"
                    ),
                    {"blueprint": '{"name":"Tampered"}', "draft_id": valid_draft_id},
                )
            await transaction.rollback()
            trigger_exists = (
                await connection.execute(
                    text("SELECT count(*) FROM pg_trigger WHERE tgname = :name AND NOT tgisinternal"),
                    {"name": IMMUTABILITY_TRIGGER},
                )
            ).scalar_one()
            assert trigger_exists == 1
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", PARENT_REVISION)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            preserved_trigger = (
                await connection.execute(
                    text("SELECT count(*) FROM pg_trigger WHERE tgname = :name AND NOT tgisinternal"),
                    {"name": IMMUTABILITY_TRIGGER},
                )
            ).scalar_one()
            preserved_audit = (
                await connection.execute(
                    text("SELECT count(*) FROM audit_logs WHERE action = 'migration.hr_runtime_authority_quarantined'")
                )
            ).scalar_one()
        assert preserved_trigger == 1
        assert preserved_audit == 1
    finally:
        await engine.dispose()
