from __future__ import annotations

from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "session_v2_0716.py"
INPUT_CONTROL_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "session_v2_input_control_0716.py"
)
ADMISSION_REVISION_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "session_v2_admission_revision_0716.py"
)

SESSION_V2_TRIGGER_FUNCTIONS = (
    "enforce_session_event_v2_contract",
    "enforce_session_writer_epoch",
    "enforce_session_v2_tenant_binding",
)

SESSION_V2_EXISTING_TRANSCRIPT_INDEXES = (
    "ix_chat_transcript_events_item_id",
    "ix_chat_transcript_events_command_id",
    "ix_chat_transcript_events_input_id",
    "ix_chat_transcript_events_result_id",
    "ix_chat_transcript_events_invocation_id",
    "uq_chat_transcript_tool_result_invocation",
)

SESSION_V2_PARENT_REVISION = "hr_runtime_authority_0715"
SESSION_V2_HEAD_REVISION = "session_v2_delete_fk_indexes_0907"


def test_session_v2_migration_is_the_single_head_and_secure_downgrade_preserves_evidence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "session_v2_0716"' in source
    assert 'down_revision = "hr_runtime_authority_0715"' in source
    assert "enforce_session_event_v2_contract" in source
    assert "enforce_session_writer_epoch" in source
    assert "allowed_existing_generations_json" in source
    assert "schema-preserving rollback" in source
    assert "DROP TABLE" not in source.split("def downgrade()", 1)[1]
    assert "_install_generated_event_contract_trigger()" in source.split("def upgrade()", 1)[1]
    assert "def _install_event_contract_trigger" not in source
    assert "app.services" not in source
    assert "migration_snapshots.session_v2_contract_0716" in source
    assert "autocommit_block" in source
    assert source.count("CREATE INDEX CONCURRENTLY IF NOT EXISTS") == 5
    assert source.count("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS") == 1
    assert "pg_catalog.pg_index" in source
    assert "index_row.indisvalid" in source
    assert "index_row.indisready" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in source
    assert "session_v2_transcript_index_rebuild_failed" in source

    bootstrap_source = (MIGRATION.parents[2] / "app" / "db_bootstrap.py").read_text(encoding="utf-8")
    session_contract_body = bootstrap_source.split("def apply_session_v2_contracts", 1)[1].split(
        "class AlembicContextProtocol", 1
    )[0]
    assert "EVENT_KIND_MATRIX" not in session_contract_body
    assert "HOOK_BOUNDARY_MATRIX" not in session_contract_body
    assert "build_session_event_contract_function_sql" in session_contract_body


def test_session_v2_revision_snapshots_match_their_owned_live_contracts() -> None:
    from app.services.session_event_contract import (
        SESSION_V2_AUTHORITY_TABLES as live_authority_tables,
        SESSION_V2_TRIGGER_FUNCTION_SIGNATURES as live_function_signatures,
        build_session_writer_epoch_function_sql as build_live_writer_sql,
    )
    from migration_snapshots.session_v2_contract_0716 import (
        SESSION_V2_AUTHORITY_TABLES as frozen_authority_tables,
        SESSION_V2_TRIGGER_FUNCTION_SIGNATURES as frozen_function_signatures,
        build_session_writer_epoch_function_sql as build_frozen_writer_sql,
    )
    from migration_snapshots.peer_a2a_session_authority_contract_0717 import (
        build_session_event_contract_function_sql as build_frozen_event_sql,
    )
    from migration_snapshots.session_v2_admission_revision_contract_0716 import (
        build_session_tenant_binding_function_sql as build_frozen_authority_sql,
    )
    from app.services.session_event_contract import (
        build_session_event_contract_function_sql as build_live_event_sql,
        build_session_tenant_binding_function_sql as build_live_authority_sql,
    )

    assert frozen_authority_tables == live_authority_tables
    assert frozen_function_signatures == live_function_signatures
    assert build_frozen_event_sql() == build_live_event_sql()
    assert build_frozen_writer_sql() == build_live_writer_sql()
    assert build_frozen_authority_sql() == build_live_authority_sql()


def test_session_v2_input_control_migration_is_additive_and_schema_preserving() -> None:
    source = INPUT_CONTROL_MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "session_v2_input_control_0716"' in source
    assert 'down_revision = "session_v2_0716"' in source
    assert "session_v2_input_control_contract_0716" in source
    assert 'alter_column("session_turn_replacements", "cancel_control_id", nullable=True)' in source
    assert 'alter_column("session_turn_replacements", "cancel_command_id", nullable=True)' in source
    downgrade = source.split("def downgrade()", 1)[1]
    assert "session_v2_input_control_downgrade_blocked" in source
    assert "DROP TABLE" not in downgrade
    assert "DELETE FROM session_turn_replacements" not in downgrade
    assert "UPDATE session_turn_replacements" not in downgrade


def test_session_v2_admission_revision_migration_is_additive_and_schema_preserving() -> None:
    source = ADMISSION_REVISION_MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "session_v2_admission_revision_0716"' in source
    assert 'down_revision = "session_v2_input_control_0716"' in source
    assert "SET input_revision=1" in source
    assert "SET input_revision=input.revision" not in source
    assert "legacy_input_revision_admission_ambiguous" in source
    assert "uq_session_input_admissions_input_revision" in source
    downgrade = source.split("def downgrade()", 1)[1]
    assert "session_v2_admission_revision_downgrade_blocked" in downgrade
    assert "input_revision > 1" in downgrade
    assert "GROUP BY input_id HAVING count(*) > 1" in downgrade
    assert "DELETE FROM" not in downgrade


@pytest.mark.asyncio
async def test_session_v2_input_control_exact_parent_upgrade_is_nullable_and_current_head(
    session_v2_input_control_parent_migrated_pg_url: str,
) -> None:
    engine = create_async_engine(session_v2_input_control_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == SESSION_V2_HEAD_REVISION
            nullable = dict(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT column_name,is_nullable
                            FROM information_schema.columns
                            WHERE table_schema='public'
                              AND table_name='session_turn_replacements'
                              AND column_name IN ('cancel_control_id','cancel_command_id')
                            """
                        )
                    )
                ).all()
            )
            assert nullable == {"cancel_control_id": "YES", "cancel_command_id": "YES"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_v2_admission_revision_backfills_legacy_attempt_without_relabeling_current_bytes(
    session_v2_admission_revision_parent_pg_url: str,
) -> None:
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.migrations.conftest import (
        _alembic_downgrade,
        _alembic_upgrade,
        insert_chat_session_at_schema_revision,
    )

    tenant_id, user_id, agent_id, session_id, command_id, input_id, admission_id = (uuid.uuid4() for _ in range(7))
    original_hook_run_id = uuid.uuid4()
    engine = create_async_engine(session_v2_admission_revision_parent_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            db.add(Tenant(id=tenant_id, name="Admission Revision", slug=f"admission-rev-{tenant_id.hex[:8]}"))
            db.add(
                User(
                    id=user_id,
                    username=f"admission-rev-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@admission-revision.test",
                    password_hash="x",
                    display_name="Admission Revision",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Admission Revision Agent", creator_id=user_id))
            await db.flush()
            await insert_chat_session_at_schema_revision(
                db,
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            await db.execute(
                text(
                    """
                    INSERT INTO session_commands(
                      id,tenant_id,principal_id,session_id,namespace,idempotency_key,
                      command_kind,request_hash,target_hash,request_json,target_json,
                      status,receipt_ref
                    ) VALUES (
                      :id,:tenant_id,:principal_id,:session_id,'human_input',:idempotency_key,
                      'start_turn',:request_hash,:target_hash,'{}'::jsonb,'{}'::jsonb,
                      'accepted',:receipt_ref
                    )
                    """
                ),
                {
                    "id": command_id,
                    "tenant_id": tenant_id,
                    "principal_id": user_id,
                    "session_id": session_id,
                    "idempotency_key": f"legacy:{input_id}",
                    "request_hash": "a" * 64,
                    "target_hash": "b" * 64,
                    "receipt_ref": f"session-command:{command_id}",
                },
            )
            await db.execute(
                text(
                    """
                    INSERT INTO session_turn_inputs(
                      id,tenant_id,session_id,command_id,intent,content_parts_json,
                      content_hash,queue_priority,queue_ordinal,revision,status
                    ) VALUES (
                      :id,:tenant_id,:session_id,:command_id,'start_turn',
                      '[{"type":"text","text":"revision three"}]'::jsonb,
                      :content_hash,'next',1,3,'accepted'
                    )
                    """
                ),
                {
                    "id": input_id,
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "command_id": command_id,
                    "content_hash": "c" * 64,
                },
            )
            await db.execute(
                text(
                    """
                    INSERT INTO session_input_admissions(
                      id,tenant_id,session_id,command_id,input_id,state,hook_run_id,
                      hook_idempotency_key,hook_result_hash,additional_context_refs_json,
                      carry_forward
                    ) VALUES (
                      :id,:tenant_id,:session_id,:command_id,:input_id,'admitted',:hook_run_id,
                      :hook_idempotency_key,:hook_result_hash,'[]'::jsonb,'none'
                    )
                    """
                ),
                {
                    "id": admission_id,
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "command_id": command_id,
                    "input_id": input_id,
                    "hook_run_id": original_hook_run_id,
                    "hook_idempotency_key": f"legacy-hook:{input_id}",
                    "hook_result_hash": "d" * 64,
                },
            )
            await db.commit()
    finally:
        await engine.dispose()

    _alembic_upgrade(
        session_v2_admission_revision_parent_pg_url,
        "session_v2_admission_revision_0716",
    )

    engine = create_async_engine(session_v2_admission_revision_parent_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "session_v2_admission_revision_0716"
            )
            backfill = (
                await connection.execute(
                    text(
                        """
                        SELECT admission.input_revision,admission.state,admission.hook_run_id,
                               input.revision,input.status,input.recovery_owner,
                               command.status,command.rejection_json->>'reason_code'
                        FROM session_input_admissions AS admission
                        JOIN session_turn_inputs AS input ON input.id=admission.input_id
                        JOIN session_commands AS command ON command.id=admission.command_id
                        WHERE admission.id=:admission_id
                        """
                    ),
                    {"admission_id": admission_id},
                )
            ).one()
            assert tuple(backfill) == (
                1,
                "admitted",
                original_hook_run_id,
                3,
                "needs_reconciliation",
                "session_v2_admission_revision_backfill",
                "needs_reconciliation",
                "legacy_input_revision_admission_ambiguous",
            )
            constraints = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT conname FROM pg_constraint
                            WHERE conrelid='session_input_admissions'::regclass
                              AND conname LIKE 'uq_session_input_admissions_input%'
                            """
                        )
                    )
                ).all()
            }
            assert constraints == {"uq_session_input_admissions_input_revision"}

        second_admission_id = uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO session_input_admissions(
                      id,tenant_id,session_id,command_id,input_id,input_revision,state,
                      hook_run_id,hook_idempotency_key,additional_context_refs_json,carry_forward
                    ) VALUES (
                      :id,:tenant_id,:session_id,:command_id,:input_id,2,'admission_pending',
                      :hook_run_id,:hook_idempotency_key,'[]'::jsonb,'none'
                    )
                    """
                ),
                {
                    "id": second_admission_id,
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "command_id": command_id,
                    "input_id": input_id,
                    "hook_run_id": uuid.uuid4(),
                    "hook_idempotency_key": f"revision-two:{input_id}",
                },
            )
    finally:
        await engine.dispose()

    with pytest.raises(pytest.fail.Exception, match="session_v2_admission_revision_downgrade_blocked"):
        _alembic_downgrade(session_v2_admission_revision_parent_pg_url, "session_v2_input_control_0716")

    engine = create_async_engine(session_v2_admission_revision_parent_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "session_v2_admission_revision_0716"
            )
            attempts = (
                await connection.execute(
                    text(
                        """
                        SELECT input_revision,state FROM session_input_admissions
                        WHERE input_id=:input_id ORDER BY input_revision
                        """
                    ),
                    {"input_id": input_id},
                )
            ).all()
            assert [tuple(row) for row in attempts] == [(1, "admitted"), (2, "admission_pending")]
    finally:
        await engine.dispose()


def test_migration_downgrade_is_a_fence_relaxation_not_evidence_destruction(monkeypatch) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("session_v2_0716_test", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: statements.append(str(statement)))

    module.downgrade()

    joined = "\n".join(statements)
    assert statements[0].startswith("LOCK TABLE session_writer_epochs")
    assert "session_v2_downgrade_blocked" in statements[1]
    assert "enforcement_mode='observe'" in statements[2]
    assert "enforcement_mode='observe'" in joined
    assert "DROP TRIGGER IF EXISTS trg_session_writer_epoch" in joined
    assert "DROP TRIGGER IF EXISTS trg_session_event_v2_contract" in joined
    assert "DROP TABLE" not in joined
    assert "DELETE FROM chat_transcript_events" not in joined


def test_alembic_uses_normal_bootstrap_path_without_a_production_force_chain_switch() -> None:
    alembic_env = (MIGRATION.parents[1] / "env.py").read_text(encoding="utf-8")
    fixture_source = (Path(__file__).with_name("conftest.py")).read_text(encoding="utf-8")

    assert "HIVE_ALEMBIC_FORCE_CHAIN" not in alembic_env
    assert "HIVE_ALEMBIC_FORCE_CHAIN" not in fixture_source
    assert "revision_parent_migrated_pg_url" in fixture_source


@pytest.mark.asyncio
@pytest.mark.parametrize("url_fixture", ["migrated_pg_url", "revision_parent_migrated_pg_url"])
async def test_fresh_and_upgrade_paths_install_the_same_session_v2_authorities(request, url_fixture: str) -> None:
    database_url = request.getfixturevalue(url_fixture)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            tables = {
                row[0]
                for row in (
                    await connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'session_%'")
                    )
                ).all()
            }
            assert {
                "session_commands",
                "session_event_cursors",
                "session_event_outbox",
                "session_turn_inputs",
                "session_input_admissions",
                "session_control_inputs",
                "session_turn_replacements",
                "session_tool_invocations",
                "session_model_results",
                "session_run_outcomes",
                "session_feedback_aggregates",
                "session_writer_epochs",
            } <= tables
            columns = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_name='chat_transcript_events'"
                        )
                    )
                ).all()
            }
            assert {
                "item_id",
                "item_kind",
                "lifecycle",
                "payload_schema",
                "scope_json",
                "command_id",
                "result_id",
            } <= columns
            triggers = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname IN ('trg_session_event_v2_contract','trg_session_writer_epoch')"
                        )
                    )
                ).all()
            }
            all_triggers = [
                row[0]
                for row in (
                    await connection.execute(
                        text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname")
                    )
                ).all()
            ]
            alembic_version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert triggers == {"trg_session_event_v2_contract", "trg_session_writer_epoch"}, (
                alembic_version,
                all_triggers,
            )
            tenant_binding_triggers = {
                row[0]
                for row in (
                    await connection.execute(
                        text("""
                          SELECT tgname FROM pg_trigger
                          WHERE NOT tgisinternal AND tgname LIKE 'trg_session_%_tenant_binding'
                        """)
                    )
                ).all()
            }
            assert tenant_binding_triggers == {
                "trg_session_event_cursors_tenant_binding",
                "trg_session_event_outbox_tenant_binding",
                "trg_session_commands_tenant_binding",
                "trg_session_turn_inputs_tenant_binding",
                "trg_session_input_admissions_tenant_binding",
                "trg_session_carry_forwards_tenant_binding",
                "trg_session_control_inputs_tenant_binding",
                "trg_session_turn_replacements_tenant_binding",
                "trg_session_tool_invocations_tenant_binding",
                "trg_session_model_results_tenant_binding",
                "trg_session_round_obligations_tenant_binding",
                "trg_session_next_round_plans_tenant_binding",
                "trg_session_run_outcomes_tenant_binding",
                "trg_session_feedback_aggregates_tenant_binding",
            }
            forced = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT relname, relforcerowsecurity FROM pg_class WHERE relname IN ('session_commands','session_event_outbox','session_model_results')"
                        )
                    )
                ).all()
            )
            assert forced == {"session_commands": True, "session_event_outbox": True, "session_model_results": True}
            epoch = (
                await connection.execute(
                    text(
                        "SELECT state,new_run_generation,allowed_existing_generations_json,enforcement_mode FROM session_writer_epochs WHERE id='global'"
                    )
                )
            ).one()
            assert tuple(epoch[:2]) == ("legacy_open", 1)
            assert epoch.allowed_existing_generations_json == [1]
            assert epoch.enforcement_mode == "observe"
            function_def = await connection.scalar(
                text("SELECT pg_get_functiondef('enforce_session_event_v2_contract()'::regprocedure)")
            )
            from app.services.session_event_contract import EVENT_KIND_MATRIX, HOOK_BOUNDARY_MATRIX

            assert function_def is not None
            for item_kind, rule in EVENT_KIND_MATRIX.items():
                assert f'"{item_kind}"' in function_def
                for lifecycle in rule.lifecycles:
                    assert f'"{lifecycle}"' in function_def
            for boundary in HOOK_BOUNDARY_MATRIX:
                assert f'"{boundary}"' in function_def
            authority_function_def = await connection.scalar(
                text("SELECT pg_get_functiondef('enforce_session_v2_tenant_binding()'::regprocedure)")
            )
            from app.services.session_event_contract import SESSION_V2_AUTHORITY_TABLES

            assert authority_function_def is not None
            assert "SECURITY DEFINER" in authority_function_def
            assert "session_v2_authority_binding_mismatch" in authority_function_def
            for table_name in SESSION_V2_AUTHORITY_TABLES:
                assert f"TG_TABLE_NAME='{table_name}'" in authority_function_def
            function_security = (
                await connection.execute(
                    text(
                        """
                        SELECT procedure.proname,
                               procedure.prosecdef,
                               COALESCE(procedure.proconfig, ARRAY[]::text[]),
                               EXISTS (
                                 SELECT 1
                                 FROM aclexplode(
                                   COALESCE(
                                     procedure.proacl,
                                     acldefault('f', procedure.proowner)
                                   )
                                 ) AS privilege
                                 WHERE privilege.grantee=0
                                   AND privilege.privilege_type='EXECUTE'
                               ) AS public_execute,
                               pg_get_functiondef(procedure.oid)
                        FROM pg_proc AS procedure
                        JOIN pg_namespace AS namespace
                          ON namespace.oid=procedure.pronamespace
                        WHERE namespace.nspname='public'
                          AND procedure.proname=ANY(:function_names)
                        ORDER BY procedure.proname
                        """
                    ),
                    {"function_names": list(SESSION_V2_TRIGGER_FUNCTIONS)},
                )
            ).all()
            assert {row.proname for row in function_security} == set(SESSION_V2_TRIGGER_FUNCTIONS)
            for row in function_security:
                assert row.prosecdef is True, row.proname
                assert list(row.coalesce) == ["search_path=pg_catalog"], row.proname
                assert row.public_execute is False, row.proname
                assert f"FUNCTION public.{row.proname}()" in row.pg_get_functiondef
                assert "SET search_path TO 'pg_catalog'" in row.pg_get_functiondef
                assert "pg_catalog, public" not in row.pg_get_functiondef
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_and_revision_parent_paths_install_byte_identical_authority_functions(
    migrated_pg_url,
    revision_parent_migrated_pg_url,
) -> None:
    definitions: list[dict[str, str]] = []
    for database_url in (migrated_pg_url, revision_parent_migrated_pg_url):
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            """
                            SELECT procedure.proname,pg_get_functiondef(procedure.oid)
                            FROM pg_proc AS procedure
                            JOIN pg_namespace AS namespace
                              ON namespace.oid=procedure.pronamespace
                            WHERE namespace.nspname='public'
                              AND procedure.proname=ANY(:function_names)
                            ORDER BY procedure.proname
                            """
                        ),
                        {"function_names": list(SESSION_V2_TRIGGER_FUNCTIONS)},
                    )
                ).all()
                definitions.append({row.proname: row.pg_get_functiondef for row in rows})
        finally:
            await engine.dispose()
    assert set(definitions[0]) == set(SESSION_V2_TRIGGER_FUNCTIONS)
    assert definitions[0] == definitions[1]


@pytest.mark.asyncio
async def test_app_role_trigger_execution_survives_public_execute_revocation(
    owner_sessionmaker,
    app_user_sessionmaker,
) -> None:
    """Trigger invocation remains valid while direct PUBLIC execution is denied."""

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Trigger App Role", slug=f"trigger-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"trigger-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@trigger-app-role.test",
                password_hash="x",
                display_name="Trigger App Role",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Trigger Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, tenant_id=tenant_id, agent_id=agent_id, user_id=user_id))
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        privileges = (
            await db.execute(
                text(
                    """
                    SELECT procedure.proname,
                           has_function_privilege(
                             current_user,
                             procedure.oid,
                             'EXECUTE'
                           ) AS can_execute
                    FROM pg_proc AS procedure
                    JOIN pg_namespace AS namespace
                      ON namespace.oid=procedure.pronamespace
                    WHERE namespace.nspname='public'
                      AND procedure.proname=ANY(:function_names)
                    ORDER BY procedure.proname
                    """
                ),
                {"function_names": list(SESSION_V2_TRIGGER_FUNCTIONS)},
            )
        ).all()
        assert {row.proname for row in privileges} == set(SESSION_V2_TRIGGER_FUNCTIONS)
        assert all(row.can_execute is False for row in privileges)

        await db.execute(
            text(
                """
                INSERT INTO session_event_cursors(session_id,tenant_id,next_sequence,version)
                VALUES (:session_id,:tenant_id,1,1)
                """
            ),
            {"session_id": session_id, "tenant_id": tenant_id},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_fresh_and_revision_parent_paths_have_identical_session_v2_schema_contracts(
    migrated_pg_url,
    revision_parent_migrated_pg_url,
) -> None:
    from app.services.session_event_contract import SESSION_V2_AUTHORITY_TABLES

    tables = (*SESSION_V2_AUTHORITY_TABLES, "session_writer_epochs", "session_writer_heartbeats")

    async def catalog(database_url: str) -> dict[str, set[str]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                constraints = (
                    await connection.execute(
                        text("""
                          SELECT table_class.relname, constraint_row.contype,
                                 pg_get_constraintdef(constraint_row.oid, true)
                          FROM pg_constraint AS constraint_row
                          JOIN pg_class AS table_class
                            ON table_class.oid=constraint_row.conrelid
                          JOIN pg_namespace AS table_namespace
                            ON table_namespace.oid=table_class.relnamespace
                          WHERE table_namespace.nspname='public'
                            AND table_class.relname=ANY(:tables)
                            AND constraint_row.contype IN ('c','u')
                        """),
                        {"tables": list(tables)},
                    )
                ).all()
                indexes = (
                    await connection.execute(
                        text("""
                          SELECT table_class.relname,index_row.indisunique,index_row.indisprimary,
                                 pg_get_indexdef(index_row.indexrelid)
                          FROM pg_index AS index_row
                          JOIN pg_class AS table_class ON table_class.oid=index_row.indrelid
                          JOIN pg_namespace AS table_namespace
                            ON table_namespace.oid=table_class.relnamespace
                          WHERE table_namespace.nspname='public'
                            AND table_class.relname=ANY(:tables)
                        """),
                        {"tables": list(tables)},
                    )
                ).all()
        finally:
            await engine.dispose()

        normalized: dict[str, set[str]] = {table_name: set() for table_name in tables}
        for table_name, constraint_type, definition in constraints:
            normalized[table_name].add(f"constraint:{constraint_type}:{' '.join(str(definition).split())}")
        for table_name, unique, primary, definition in indexes:
            _prefix, on_clause = str(definition).split(" ON ", 1)
            normalized[table_name].add(
                f"index:unique={bool(unique)}:primary={bool(primary)}:{' '.join(on_clause.split())}"
            )
        return normalized

    fresh = await catalog(migrated_pg_url)
    upgraded = await catalog(revision_parent_migrated_pg_url)
    assert fresh == upgraded, {
        table_name: {
            "fresh_only": sorted(fresh[table_name] - upgraded[table_name]),
            "upgrade_only": sorted(upgraded[table_name] - fresh[table_name]),
        }
        for table_name in tables
        if fresh[table_name] != upgraded[table_name]
    }


@pytest.mark.asyncio
async def test_existing_transcript_indexes_are_valid_and_match_fresh_after_concurrent_upgrade(
    migrated_pg_url,
    revision_parent_migrated_pg_url,
) -> None:
    async def catalog(database_url: str) -> dict[str, tuple[bool, bool, bool, str, str | None]]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            """
                            SELECT index_class.relname,
                                   index_row.indisvalid,
                                   index_row.indisready,
                                   index_row.indisunique,
                                   pg_get_indexdef(index_row.indexrelid),
                                   pg_get_expr(index_row.indpred,index_row.indrelid)
                            FROM pg_index AS index_row
                            JOIN pg_class AS index_class
                              ON index_class.oid=index_row.indexrelid
                            JOIN pg_namespace AS index_namespace
                              ON index_namespace.oid=index_class.relnamespace
                            WHERE index_namespace.nspname='public'
                              AND index_class.relname=ANY(:index_names)
                            ORDER BY index_class.relname
                            """
                        ),
                        {"index_names": list(SESSION_V2_EXISTING_TRANSCRIPT_INDEXES)},
                    )
                ).all()
        finally:
            await engine.dispose()
        return {
            row.relname: (
                row.indisvalid,
                row.indisready,
                row.indisunique,
                " ".join(row.pg_get_indexdef.split()),
                " ".join(row.pg_get_expr.split()) if row.pg_get_expr else None,
            )
            for row in rows
        }

    fresh = await catalog(migrated_pg_url)
    upgraded = await catalog(revision_parent_migrated_pg_url)
    assert set(fresh) == set(SESSION_V2_EXISTING_TRANSCRIPT_INDEXES)
    assert upgraded == fresh
    assert all(valid and ready for valid, ready, _unique, _definition, _predicate in upgraded.values())
    for index_name, (_valid, _ready, unique, _definition, predicate) in upgraded.items():
        assert unique is (index_name == "uq_chat_transcript_tool_result_invocation")
        if unique:
            assert predicate == (
                "((schema_version = 2) AND ((item_kind)::text = 'tool_result'::text) "
                "AND ((lifecycle)::text = 'completed'::text))"
            )
        else:
            assert predicate is None


@pytest.mark.asyncio
async def test_revision_recovers_invalid_concurrent_index_residue_without_rebuilding_valid_sibling(
    session_v2_invalid_index_pg_url,
) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    tenant_id, user_id, agent_id, session_id, invocation_id = (uuid.uuid4() for _ in range(5))
    event_ids = (uuid.uuid4(), uuid.uuid4())
    engine = create_async_engine(session_v2_invalid_index_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            db.add(
                Tenant(
                    id=tenant_id,
                    name="Invalid Index Recovery",
                    slug=f"invalid-index-{tenant_id.hex[:8]}",
                )
            )
            db.add(
                User(
                    id=user_id,
                    username=f"invalid-index-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@invalid-index.test",
                    password_hash="x",
                    display_name="Invalid Index Recovery",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name="Invalid Index Agent",
                    creator_id=user_id,
                )
            )
            await db.flush()
            db.add(
                ChatSession(
                    id=session_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            )
            await db.commit()

        async with engine.connect() as connection:
            sibling_oid = await connection.scalar(
                text("SELECT 'public.ix_chat_transcript_events_item_id'::regclass::oid")
            )
            assert sibling_oid is not None

        async with engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text("DROP INDEX CONCURRENTLY public.uq_chat_transcript_tool_result_invocation"))

        async with session_factory() as db:
            await db.execute(text("ALTER TABLE public.chat_transcript_events DISABLE TRIGGER USER"))
            for sequence, event_id in enumerate(event_ids, start=1):
                await db.execute(
                    text(
                        """
                        INSERT INTO public.chat_transcript_events(
                          id,sequence,tenant_id,agent_id,session_id,schema_version,
                          item_id,item_kind,lifecycle,payload_schema,scope_json,
                          invocation_id,actor_type,event_type,visibility_scope,
                          listed_surface,metadata_json
                        ) VALUES (
                          :id,:sequence,:tenant_id,:agent_id,:session_id,2,
                          :item_id,'tool_result','completed',
                          'hive.session.payload.tool_result.completed.v2',
                          CAST(:scope_json AS jsonb),:invocation_id,'runtime',
                          'tool_result.completed','direct_user','chat',
                          '{"v2_payload":{},"actor":{},"visibility":{}}'::jsonb
                        )
                        """
                    ),
                    {
                        "id": event_id,
                        "sequence": sequence,
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "item_id": uuid.uuid4(),
                        "scope_json": (
                            '{"level":"session","session_id":"'
                            + str(session_id)
                            + '","thread_id":"'
                            + str(session_id)
                            + '"}'
                        ),
                        "invocation_id": invocation_id,
                    },
                )
            await db.execute(text("ALTER TABLE public.chat_transcript_events ENABLE TRIGGER USER"))
            await db.commit()

        with pytest.raises(Exception):
            async with engine.connect() as connection:
                autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await autocommit.execute(
                    text(
                        """
                        CREATE UNIQUE INDEX CONCURRENTLY uq_chat_transcript_tool_result_invocation
                        ON public.chat_transcript_events(session_id,invocation_id)
                        WHERE schema_version=2
                          AND item_kind='tool_result'
                          AND lifecycle='completed'
                        """
                    )
                )

        async with engine.connect() as connection:
            invalid_residue = (
                await connection.execute(
                    text(
                        """
                        SELECT index_class.oid,index_row.indisvalid,index_row.indisready
                        FROM pg_catalog.pg_index AS index_row
                        JOIN pg_catalog.pg_class AS index_class
                          ON index_class.oid=index_row.indexrelid
                        JOIN pg_catalog.pg_namespace AS namespace
                          ON namespace.oid=index_class.relnamespace
                        WHERE namespace.nspname='public'
                          AND index_class.relname='uq_chat_transcript_tool_result_invocation'
                        """
                    )
                )
            ).one()
            assert not (invalid_residue.indisvalid and invalid_residue.indisready)
            invalid_oid = invalid_residue.oid

        async with session_factory() as db:
            await db.execute(
                text("DELETE FROM public.chat_transcript_events WHERE id=ANY(:event_ids)"),
                {"event_ids": list(event_ids)},
            )
            await db.commit()
    finally:
        await engine.dispose()

    _alembic_downgrade(session_v2_invalid_index_pg_url, SESSION_V2_PARENT_REVISION)
    _alembic_upgrade(session_v2_invalid_index_pg_url, "head")

    verified = create_async_engine(session_v2_invalid_index_pg_url, poolclass=NullPool)
    try:
        async with verified.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT index_class.relname,index_class.oid,index_row.indisvalid,
                               index_row.indisready,index_row.indisunique,
                               pg_get_expr(index_row.indpred,index_row.indrelid) AS predicate
                        FROM pg_catalog.pg_index AS index_row
                        JOIN pg_catalog.pg_class AS index_class
                          ON index_class.oid=index_row.indexrelid
                        JOIN pg_catalog.pg_namespace AS namespace
                          ON namespace.oid=index_class.relnamespace
                        WHERE namespace.nspname='public'
                          AND index_class.relname=ANY(:index_names)
                        ORDER BY index_class.relname
                        """
                    ),
                    {
                        "index_names": [
                            "ix_chat_transcript_events_item_id",
                            "uq_chat_transcript_tool_result_invocation",
                        ]
                    },
                )
            ).all()
            catalog = {row.relname: row for row in rows}
            sibling = catalog["ix_chat_transcript_events_item_id"]
            rebuilt = catalog["uq_chat_transcript_tool_result_invocation"]
            assert sibling.oid == sibling_oid
            assert rebuilt.oid != invalid_oid
            assert rebuilt.indisvalid is True
            assert rebuilt.indisready is True
            assert rebuilt.indisunique is True
            assert rebuilt.predicate == (
                "((schema_version = 2) AND ((item_kind)::text = 'tool_result'::text) "
                "AND ((lifecycle)::text = 'completed'::text))"
            )
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == SESSION_V2_HEAD_REVISION
    finally:
        await verified.dispose()


@pytest.mark.asyncio
async def test_schema_preserving_downgrade_and_reupgrade_keep_session_v2_evidence(
    session_v2_roundtrip_pg_url,
) -> None:
    """Exercise the real parent/head round trip without deleting V2 evidence."""

    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.migrations.conftest import (
        _alembic_downgrade,
        _alembic_upgrade,
    )

    tenant_id, user_id, agent_id, session_id, command_id = (uuid.uuid4() for _ in range(5))
    engine = create_async_engine(session_v2_roundtrip_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            db.add(
                Tenant(
                    id=tenant_id,
                    name="Session V2 Roundtrip",
                    slug=f"session-v2-roundtrip-{tenant_id.hex[:8]}",
                )
            )
            db.add(
                User(
                    id=user_id,
                    username=f"session-v2-roundtrip-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@session-v2-roundtrip.test",
                    password_hash="x",
                    display_name="Session V2 Roundtrip",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name="Session V2 Roundtrip Agent",
                    creator_id=user_id,
                )
            )
            await db.flush()
            db.add(
                ChatSession(
                    id=session_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            )
            await db.flush()
            await db.execute(
                text(
                    """
                    INSERT INTO session_commands(
                      id,tenant_id,principal_id,session_id,namespace,idempotency_key,
                      command_kind,request_hash,target_hash,request_json,target_json,
                      status,receipt_ref
                    ) VALUES (
                      :id,:tenant_id,:principal_id,:session_id,'human_input',:key,
                      'start_turn',repeat('a',64),repeat('b',64),
                      '{"evidence":"preserve-me"}'::jsonb,'{}'::jsonb,
                      'accepted','roundtrip-fixture'
                    )
                    """
                ),
                {
                    "id": command_id,
                    "tenant_id": tenant_id,
                    "principal_id": user_id,
                    "session_id": session_id,
                    "key": f"roundtrip-{command_id}",
                },
            )
            await db.commit()
    finally:
        await engine.dispose()

    parent = SESSION_V2_PARENT_REVISION
    _alembic_downgrade(session_v2_roundtrip_pg_url, parent)

    downgraded_function_contract: dict[str, tuple[str, bool, tuple[str, ...], bool]] = {}
    downgraded = create_async_engine(session_v2_roundtrip_pg_url, poolclass=NullPool)
    try:
        async with downgraded.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == parent
            preserved = (
                await connection.execute(
                    text(
                        """
                        SELECT request_json,receipt_ref
                        FROM session_commands WHERE id=:command_id
                        """
                    ),
                    {"command_id": command_id},
                )
            ).one()
            assert preserved.request_json == {"evidence": "preserve-me"}
            assert preserved.receipt_ref == "roundtrip-fixture"
            root_triggers = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT tgname FROM pg_trigger
                            WHERE NOT tgisinternal
                              AND tgname IN (
                                'trg_session_event_v2_contract',
                                'trg_session_writer_epoch'
                              )
                            """
                        )
                    )
                ).all()
            }
            assert root_triggers == set()
            tenant_binding_trigger_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgname LIKE 'trg_session_%_tenant_binding'"
                )
            )
            assert tenant_binding_trigger_count == 14
            function_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT procedure.proname,
                               pg_get_functiondef(procedure.oid),
                               procedure.prosecdef,
                               COALESCE(procedure.proconfig, ARRAY[]::text[]),
                               EXISTS (
                                 SELECT 1
                                 FROM aclexplode(
                                   COALESCE(
                                     procedure.proacl,
                                     acldefault('f', procedure.proowner)
                                   )
                                 ) AS privilege
                                 WHERE privilege.grantee=0
                                   AND privilege.privilege_type='EXECUTE'
                               ) AS public_execute
                        FROM pg_proc AS procedure
                        JOIN pg_namespace AS namespace
                          ON namespace.oid=procedure.pronamespace
                        WHERE namespace.nspname='public'
                          AND procedure.proname=ANY(:function_names)
                        ORDER BY procedure.proname
                        """
                    ),
                    {"function_names": list(SESSION_V2_TRIGGER_FUNCTIONS)},
                )
            ).all()
            downgraded_function_contract = {
                row.proname: (
                    row.pg_get_functiondef,
                    row.prosecdef,
                    tuple(row.coalesce),
                    row.public_execute,
                )
                for row in function_rows
            }
            assert set(downgraded_function_contract) == set(SESSION_V2_TRIGGER_FUNCTIONS)
            for function_name, contract in downgraded_function_contract.items():
                definition, security_definer, function_config, public_execute = contract
                assert "SECURITY DEFINER" in definition, function_name
                assert security_definer is True, function_name
                assert function_config == ("search_path=pg_catalog",), function_name
                assert public_execute is False, function_name
            assert (
                await connection.scalar(
                    text(
                        """
                        SELECT enforcement_mode FROM session_writer_epochs
                        WHERE id='global'
                        """
                    )
                )
            ) == "observe"
    finally:
        await downgraded.dispose()

    _alembic_upgrade(session_v2_roundtrip_pg_url, "head")

    restored = create_async_engine(session_v2_roundtrip_pg_url, poolclass=NullPool)
    try:
        async with restored.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == SESSION_V2_HEAD_REVISION
            assert (
                await connection.scalar(
                    text("SELECT request_json->>'evidence' FROM session_commands WHERE id=:command_id"),
                    {"command_id": command_id},
                )
            ) == "preserve-me"
            restored_triggers = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT tgname FROM pg_trigger
                            WHERE NOT tgisinternal
                              AND tgname IN (
                                'trg_session_event_v2_contract',
                                'trg_session_writer_epoch'
                              )
                            """
                        )
                    )
                ).all()
            }
            assert restored_triggers == {
                "trg_session_event_v2_contract",
                "trg_session_writer_epoch",
            }
            restored_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT procedure.proname,
                               pg_get_functiondef(procedure.oid),
                               procedure.prosecdef,
                               COALESCE(procedure.proconfig, ARRAY[]::text[]),
                               EXISTS (
                                 SELECT 1
                                 FROM aclexplode(
                                   COALESCE(
                                     procedure.proacl,
                                     acldefault('f', procedure.proowner)
                                   )
                                 ) AS privilege
                                 WHERE privilege.grantee=0
                                   AND privilege.privilege_type='EXECUTE'
                               ) AS public_execute
                        FROM pg_proc AS procedure
                        JOIN pg_namespace AS namespace
                          ON namespace.oid=procedure.pronamespace
                        WHERE namespace.nspname='public'
                          AND procedure.proname=ANY(:function_names)
                        ORDER BY procedure.proname
                        """
                    ),
                    {"function_names": list(SESSION_V2_TRIGGER_FUNCTIONS)},
                )
            ).all()
            restored_function_contract = {
                row.proname: (
                    row.pg_get_functiondef,
                    row.prosecdef,
                    tuple(row.coalesce),
                    row.public_execute,
                )
                for row in restored_rows
            }
            assert set(restored_function_contract) == set(SESSION_V2_TRIGGER_FUNCTIONS)
            for function_name, contract in restored_function_contract.items():
                definition, security_definer, function_config, public_execute = contract
                assert "SECURITY DEFINER" in definition, function_name
                assert security_definer is True, function_name
                assert function_config == ("search_path=pg_catalog",), function_name
                assert public_execute is False, function_name
    finally:
        await restored.dispose()


@pytest.mark.asyncio
async def test_downgrade_rejects_any_v2_event_even_before_cutover(
    migrated_pg_url,
) -> None:
    """A schema-v2 fact is not mechanically provable as safe for an older binary."""

    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.migrations.conftest import (
        _alembic_downgrade,
        _alembic_upgrade,
        insert_chat_session_at_schema_revision,
    )

    # This test belongs to the original Session V2 revision.  Put the shared
    # database on that revision explicitly before exercising its downgrade
    # guard; the additive input/control head has its own rollback contract.
    _alembic_downgrade(migrated_pg_url, "session_v2_0716")

    tenant_id, user_id, agent_id, session_id, event_id = (uuid.uuid4() for _ in range(5))
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            await db.execute(
                text("""
                  UPDATE session_writer_epochs
                  SET state='legacy_open',new_run_generation=1,
                      allowed_existing_generations_json='[1]'::jsonb,
                      enforcement_mode='observe',version=version+1,updated_at=now()
                  WHERE id='global'
                """)
            )
            db.add(Tenant(id=tenant_id, name="Pre-cutover V2", slug=f"pre-v2-{tenant_id.hex[:8]}"))
            db.add(
                User(
                    id=user_id,
                    username=f"pre-v2-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@pre-v2.test",
                    password_hash="x",
                    display_name="Pre-cutover V2",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Pre-cutover Agent", creator_id=user_id))
            await db.flush()
            await insert_chat_session_at_schema_revision(
                db,
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            await db.execute(
                text("""
                  INSERT INTO chat_transcript_events(
                    id,sequence,tenant_id,agent_id,session_id,schema_version,item_id,item_kind,
                    lifecycle,payload_schema,scope_json,item_type,item_status,actor_type,
                    event_type,visibility_scope,listed_surface,content,metadata_json,
                    projection_status,projection_attempts
                  ) VALUES (
                    :id,1,:tenant_id,:agent_id,:session_id,2,:item_id,'runtime_failure',
                    'recorded','hive.session.payload.runtime_failure.recorded.v2',
                    CAST(:scope AS jsonb),'runtime_failure','recorded','runtime',
                    'runtime_failure.recorded','operator','ops','',
                    '{"v2_payload":{"code":"pre_cutover_v2"},"actor":{"type":"runtime"},'
                    '"visibility":{"audience":"operator"}}'::jsonb,'pending',0
                  )
                """),
                {
                    "id": event_id,
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "item_id": uuid.uuid4(),
                    "scope": ('{"level":"session","session_id":"%s","thread_id":"%s"}' % (session_id, session_id)),
                },
            )
            await db.commit()
    finally:
        await engine.dispose()

    try:
        with pytest.raises(pytest.fail.Exception, match="session_v2_downgrade_blocked"):
            _alembic_downgrade(migrated_pg_url, SESSION_V2_PARENT_REVISION)

        engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "session_v2_0716"
                assert (
                    await connection.scalar(
                        text("SELECT schema_version FROM chat_transcript_events WHERE id=:id"),
                        {"id": event_id},
                    )
                    == 2
                )
        finally:
            await engine.dispose()
    finally:
        _alembic_upgrade(migrated_pg_url, "head")
        cleanup = create_async_engine(migrated_pg_url, poolclass=NullPool)
        try:
            async with cleanup.begin() as connection:
                await connection.execute(text("DELETE FROM chat_transcript_events WHERE id=:id"), {"id": event_id})
                await connection.execute(text("DELETE FROM chat_sessions WHERE id=:id"), {"id": session_id})
                await connection.execute(text("DELETE FROM agents WHERE id=:id"), {"id": agent_id})
                await connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": user_id})
                await connection.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": tenant_id})
        finally:
            await cleanup.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("epoch_state", ["v1_draining", "v2_only"])
async def test_downgrade_rejects_after_generation_two_facts_without_mutating_head(
    migrated_pg_url,
    epoch_state: str,
) -> None:
    """A V2-aware rollback artifact is mandatory after the generation-2 fence."""

    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.migrations.conftest import (
        _alembic_downgrade,
        _alembic_upgrade,
        insert_chat_session_at_schema_revision,
        insert_runtime_task_at_schema_revision,
    )

    # Keep this legacy-revision rollback proof independent from the additive
    # input/control head introduced later in the chain.
    _alembic_downgrade(migrated_pg_url, "session_v2_0716")

    tenant_id, user_id, agent_id, session_id, run_id, event_id = (uuid.uuid4() for _ in range(6))
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            await db.execute(
                text(
                    """
                    UPDATE session_writer_epochs
                    SET state='legacy_open',new_run_generation=1,
                        allowed_existing_generations_json='[1]'::jsonb,
                        enforcement_mode='observe',version=version+1,updated_at=now()
                    WHERE id='global'
                    """
                )
            )
            db.add(
                Tenant(
                    id=tenant_id,
                    name=f"Downgrade Guard {epoch_state}",
                    slug=f"downgrade-guard-{tenant_id.hex[:8]}",
                )
            )
            db.add(
                User(
                    id=user_id,
                    username=f"downgrade-guard-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@downgrade-guard.test",
                    password_hash="x",
                    display_name="Downgrade Guard",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name="Downgrade Guard Agent",
                    creator_id=user_id,
                )
            )
            await db.flush()
            await insert_chat_session_at_schema_revision(
                db,
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            await insert_runtime_task_at_schema_revision(
                db,
                id=run_id,
                task_type="web_chat_turn",
                tenant_id=tenant_id,
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                status="running",
                writer_generation=2,
                metadata_json={"session_id": str(session_id)},
            )
            await db.commit()

        async with session_factory() as db:
            await db.execute(
                text(
                    """
                    UPDATE session_writer_epochs
                    SET state=:state,new_run_generation=2,
                        allowed_existing_generations_json=CAST(:allowed AS jsonb),
                        enforcement_mode='enforce',version=version+1,updated_at=now()
                    WHERE id='global'
                    """
                ),
                {
                    "state": epoch_state,
                    "allowed": "[1,2]" if epoch_state == "v1_draining" else "[2]",
                },
            )
            await db.execute(
                text(
                    """
                    INSERT INTO chat_transcript_events(
                      id,sequence,tenant_id,agent_id,session_id,run_id,schema_version,
                      item_id,item_kind,lifecycle,payload_schema,scope_json,item_type,
                      item_status,actor_type,event_type,visibility_scope,listed_surface,
                      content,metadata_json,projection_status,projection_attempts
                    ) VALUES (
                      :id,1,:tenant_id,:agent_id,:session_id,:run_id,2,
                      :item_id,'runtime_failure','recorded',
                      'hive.session.payload.runtime_failure.recorded.v2',CAST(:scope AS jsonb),
                      'runtime_failure','recorded','runtime','runtime_failure.recorded',
                      'operator','ops','',
                      '{"v2_payload":{"domain":"migration","code":"generation_two_fact"},'
                      '"actor":{"type":"runtime"},"visibility":{"audience":"operator"}}'::jsonb,
                      'pending',0
                    )
                    """
                ),
                {
                    "id": event_id,
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "run_id": run_id,
                    "item_id": uuid.uuid4(),
                    "scope": (
                        '{"level":"run","session_id":"%s","thread_id":"%s",'
                        '"turn_id":"turn-generation-2","run_id":"%s"}' % (session_id, session_id, run_id)
                    ),
                },
            )
            await db.commit()

        async with engine.connect() as connection:
            epoch_before = (
                await connection.execute(
                    text(
                        """
                        SELECT state,new_run_generation,allowed_existing_generations_json,
                               enforcement_mode,version
                        FROM session_writer_epochs WHERE id='global'
                        """
                    )
                )
            ).one()
            triggers_before = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT tgname FROM pg_trigger
                            WHERE NOT tgisinternal
                              AND tgname IN ('trg_session_event_v2_contract','trg_session_writer_epoch')
                            """
                        )
                    )
                ).all()
            }
        await engine.dispose()

        with pytest.raises(pytest.fail.Exception, match="session_v2_downgrade_blocked"):
            _alembic_downgrade(migrated_pg_url, SESSION_V2_PARENT_REVISION)

        engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "session_v2_0716"
            assert (
                await connection.execute(
                    text(
                        """
                        SELECT state,new_run_generation,allowed_existing_generations_json,
                               enforcement_mode,version
                        FROM session_writer_epochs WHERE id='global'
                        """
                    )
                )
            ).one() == epoch_before
            triggers_after = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT tgname FROM pg_trigger
                            WHERE NOT tgisinternal
                              AND tgname IN ('trg_session_event_v2_contract','trg_session_writer_epoch')
                            """
                        )
                    )
                ).all()
            }
            assert (
                triggers_after
                == triggers_before
                == {
                    "trg_session_event_v2_contract",
                    "trg_session_writer_epoch",
                }
            )
            evidence = (
                await connection.execute(
                    text(
                        """
                        SELECT schema_version,item_kind,run_id,metadata_json->'v2_payload'->>'code'
                        FROM chat_transcript_events WHERE id=:event_id
                        """
                    ),
                    {"event_id": event_id},
                )
            ).one()
            assert tuple(evidence) == (2, "runtime_failure", run_id, "generation_two_fact")
    finally:
        await engine.dispose()
        # The pre-fix Red path actually downgraded. Re-upgrade before cleanup so
        # this shared production-shaped fixture cannot contaminate later tests.
        _alembic_upgrade(migrated_pg_url, "head")
        cleanup_engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
        try:
            async with cleanup_engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        UPDATE session_writer_epochs
                        SET state='legacy_open',new_run_generation=1,
                            allowed_existing_generations_json='[1]'::jsonb,
                            enforcement_mode='observe',version=version+1,updated_at=now()
                        WHERE id='global'
                        """
                    )
                )
                await connection.execute(
                    text("DELETE FROM chat_transcript_events WHERE id=:event_id"),
                    {"event_id": event_id},
                )
                await connection.execute(
                    text("DELETE FROM runtime_tasks WHERE id=:run_id"),
                    {"run_id": run_id},
                )
                await connection.execute(
                    text("DELETE FROM chat_sessions WHERE id=:session_id"),
                    {"session_id": session_id},
                )
                await connection.execute(
                    text("DELETE FROM agents WHERE id=:agent_id"),
                    {"agent_id": agent_id},
                )
                await connection.execute(
                    text("DELETE FROM users WHERE id=:user_id"),
                    {"user_id": user_id},
                )
                await connection.execute(
                    text("DELETE FROM tenants WHERE id=:tenant_id"),
                    {"tenant_id": tenant_id},
                )
        finally:
            await cleanup_engine.dispose()


@pytest.mark.asyncio
async def test_database_event_guard_rejects_scope_phase_and_hook_drift(migrated_pg_url) -> None:
    from sqlalchemy.exc import DBAPIError

    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    tenant_id, user_id, agent_id, session_id, run_id = (uuid.uuid4() for _ in range(5))
    try:
        from app.models.agent import Agent
        from app.models.chat_session import ChatSession
        from app.models.runtime_task import RuntimeTask
        from app.models.tenant import Tenant
        from app.models.user import User

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as db:
            db.add(Tenant(id=tenant_id, name="V2 Guard", slug=f"v2-guard-{tenant_id.hex[:8]}"))
            db.add(
                User(
                    id=user_id,
                    username=f"v2-guard-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@v2-guard.test",
                    password_hash="x",
                    display_name="V2 Guard",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add(Agent(id=agent_id, tenant_id=tenant_id, name="V2 Guard Agent", creator_id=user_id))
            await db.flush()
            db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
            db.add(
                RuntimeTask(
                    id=run_id,
                    task_type="delegation",
                    parent_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    tenant_id=tenant_id,
                    status="running",
                    writer_generation=2,
                )
            )
            await db.commit()

        insert_sql = text("""
            INSERT INTO chat_transcript_events(
              id,sequence,tenant_id,agent_id,session_id,run_id,schema_version,item_id,item_kind,lifecycle,
              payload_schema,scope_json,item_type,item_status,actor_type,event_type,visibility_scope,
              listed_surface,content,metadata_json,projection_status,projection_attempts
            ) VALUES (
              :id,:sequence,:tenant_id,:agent_id,:session_id,:run_id,2,:item_id,:item_kind,:lifecycle,
              :payload_schema,CAST(:scope_json AS jsonb),:item_kind,:lifecycle,:actor_type,:event_type,
              'direct_user','chat','',CAST(:metadata_json AS jsonb),'pending',0
            )
        """)
        base = {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "run_id": run_id,
            "item_kind": "assistant_text",
            "lifecycle": "completed",
            "payload_schema": "hive.session.payload.assistant_text.completed.v2",
            "event_type": "assistant_text.completed",
            "actor_type": "assistant",
        }

        async def assert_rejected(*, sequence: int, scope_json: str, metadata_json: str, **overrides) -> None:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                values = {
                    **base,
                    **overrides,
                    "id": uuid.uuid4(),
                    "item_id": uuid.uuid4(),
                    "sequence": sequence,
                    "scope_json": scope_json,
                    "metadata_json": metadata_json,
                }
                with pytest.raises(DBAPIError):
                    await connection.execute(insert_sql, values)
                await transaction.rollback()

        canonical_scope = (
            '{"level":"round","session_id":"%s","thread_id":"%s",'
            '"turn_id":"turn-1","run_id":"%s","round_id":"round-1"}' % (session_id, session_id, run_id)
        )
        canonical_metadata = (
            '{"v2_payload":{"phase":"unknown"},"actor":{"type":"assistant"},"visibility":{"audience":"direct_user"}}'
        )
        async with engine.begin() as connection:
            await connection.execute(
                insert_sql,
                {
                    **base,
                    "id": uuid.uuid4(),
                    "item_id": uuid.uuid4(),
                    "sequence": 1,
                    "scope_json": canonical_scope,
                    "metadata_json": canonical_metadata,
                },
            )

        await assert_rejected(
            sequence=2,
            scope_json=canonical_scope[:-1] + ',"unexpected":"drift"}',
            metadata_json=canonical_metadata,
        )
        wrong_session_scope = canonical_scope.replace(str(session_id), str(uuid.uuid4()))
        await assert_rejected(sequence=3, scope_json=wrong_session_scope, metadata_json=canonical_metadata)
        await assert_rejected(
            sequence=4,
            scope_json=canonical_scope,
            metadata_json=canonical_metadata.replace('"unknown"', '"final"'),
        )
        await assert_rejected(
            sequence=5,
            scope_json='{"level":"session","session_id":"%s","thread_id":"%s"}' % (session_id, session_id),
            metadata_json=(
                '{"v2_payload":{"boundary":"SessionStart"},"actor":{"type":"hook"},'
                '"visibility":{"audience":"operator"}}'
            ),
            item_kind="hook",
            lifecycle="started",
            payload_schema="hive.session.payload.hook.started.v2",
            event_type="hook.started",
            actor_type="hook",
            run_id=None,
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_binds_event_cursor_and_v2_rows_to_canonical_session_authority(
    migrated_pg_url,
) -> None:
    from sqlalchemy.exc import DBAPIError

    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User

    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    tenant_a, user_a, agent_a, session_a = (uuid.uuid4() for _ in range(4))
    tenant_b, user_b, agent_b, session_b = (uuid.uuid4() for _ in range(4))
    session_b_peer = uuid.uuid4()
    run_a, run_b, command_a, command_b, input_b, result_b, invocation_b = (uuid.uuid4() for _ in range(7))
    (
        admission_b,
        saga_command_b,
        cancel_command_b,
        control_b,
        carry_b,
        replacement_b,
        obligation_b,
        plan_b,
        outcome_b,
        target_event_b,
        mutation_event_b,
        target_item_b,
        mutation_item_b,
        feedback_b,
        outbox_b,
    ) = (uuid.uuid4() for _ in range(15))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def rejected(statement, values, expected: str) -> None:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(DBAPIError, match=expected):
                await connection.execute(statement, values)
            await transaction.rollback()

    try:
        async with session_factory() as db:
            db.add_all(
                [
                    Tenant(id=tenant_a, name="Binding A", slug=f"binding-a-{tenant_a.hex[:8]}"),
                    Tenant(id=tenant_b, name="Binding B", slug=f"binding-b-{tenant_b.hex[:8]}"),
                    User(
                        id=user_a,
                        username=f"binding-a-{user_a.hex[:8]}",
                        email=f"{user_a.hex[:8]}@binding-a.test",
                        password_hash="x",
                        display_name="Binding A",
                        tenant_id=tenant_a,
                    ),
                    User(
                        id=user_b,
                        username=f"binding-b-{user_b.hex[:8]}",
                        email=f"{user_b.hex[:8]}@binding-b.test",
                        password_hash="x",
                        display_name="Binding B",
                        tenant_id=tenant_b,
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    Agent(id=agent_a, tenant_id=tenant_a, name="Binding Agent A", creator_id=user_a),
                    Agent(id=agent_b, tenant_id=tenant_b, name="Binding Agent B", creator_id=user_b),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    ChatSession(id=session_a, agent_id=agent_a, tenant_id=tenant_a, user_id=user_a),
                    ChatSession(id=session_b, agent_id=agent_b, tenant_id=tenant_b, user_id=user_b),
                    ChatSession(
                        id=session_b_peer,
                        agent_id=agent_b,
                        tenant_id=tenant_b,
                        user_id=user_b,
                    ),
                ]
            )
            await db.commit()

        runtime_insert = text("""
          INSERT INTO runtime_tasks(
            id,task_type,parent_agent_id,tenant_id,status,writer_generation,parent_session_id,
            delegation_chain_json,depth,priority,attempt_count,claim_version,
            root_idempotency_key,config_snapshot_hash,policy_snapshot_hash,metadata_json
          ) VALUES (
            :id,'delegation',:agent_id,:tenant_id,'running',2,:session_id,
            '[]'::jsonb,1,0,0,0,:root_key,repeat('a',64),repeat('b',64),
            jsonb_build_object('turn_id',CAST(:turn_id AS text))
          )
        """)
        command_insert = text("""
          INSERT INTO session_commands(
            id,tenant_id,principal_id,session_id,namespace,idempotency_key,command_kind,
            request_hash,target_hash,request_json,target_json,status,receipt_ref
          ) VALUES (
            :id,:tenant_id,:principal_id,:session_id,'human_input',:key,:command_kind,
            repeat('a',64),repeat('b',64),'{}'::jsonb,'{}'::jsonb,'accepted',:receipt
          )
        """)
        async with engine.begin() as connection:
            for run_id, tenant_id, agent_id, session_id in (
                (run_a, tenant_a, agent_a, session_a),
                (run_b, tenant_b, agent_b, session_b),
            ):
                await connection.execute(
                    runtime_insert,
                    {
                        "id": run_id,
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "session_id": str(session_id),
                        "turn_id": "turn-a" if run_id == run_a else "turn-b",
                        "root_key": f"binding-run-{run_id}",
                    },
                )
            for command_id, tenant_id, principal_id, session_id in (
                (command_a, tenant_a, user_a, session_a),
                (command_b, tenant_b, user_b, session_b),
            ):
                await connection.execute(
                    command_insert,
                    {
                        "id": command_id,
                        "tenant_id": tenant_id,
                        "principal_id": principal_id,
                        "session_id": session_id,
                        "key": f"binding-command-{command_id}",
                        "command_kind": ("start_turn" if command_id == command_a else "interrupt_and_replace"),
                        "receipt": f"session-command:{command_id}",
                    },
                )
            await connection.execute(
                text("""
                  INSERT INTO session_turn_inputs(
                    id,tenant_id,session_id,command_id,intent,content_parts_json,content_hash,
                    target_turn_id,target_run_id,queue_priority,queue_ordinal,status
                  ) VALUES (
                    :id,:tenant_id,:session_id,:command_id,'interrupt_and_replace','[]'::jsonb,
                    repeat('c',64),'turn-b',:run_id,'now',1,'accepted'
                  )
                """),
                {
                    "id": input_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "command_id": command_b,
                    "run_id": run_b,
                },
            )
            for command_id, namespace, causation_id, command_kind in (
                (saga_command_b, "turn_replacement", command_b, "turn_replacement"),
                (cancel_command_b, "control_input", saga_command_b, "cancel_run"),
            ):
                await connection.execute(
                    text("""
                      INSERT INTO session_commands(
                        id,tenant_id,principal_id,session_id,namespace,causation_command_id,
                        idempotency_key,command_kind,request_hash,target_hash,request_json,
                        target_json,status,receipt_ref
                      ) VALUES (
                        :id,:tenant_id,:principal_id,:session_id,:namespace,:causation_id,
                        :key,:command_kind,repeat('a',64),repeat('b',64),'{}'::jsonb,
                        '{}'::jsonb,'accepted',:receipt
                      )
                    """),
                    {
                        "id": command_id,
                        "tenant_id": tenant_b,
                        "principal_id": user_b,
                        "session_id": session_b,
                        "namespace": namespace,
                        "causation_id": causation_id,
                        "key": f"binding-command-{command_id}",
                        "command_kind": command_kind,
                        "receipt": f"session-command:{command_id}",
                    },
                )
            await connection.execute(
                text("""
                  INSERT INTO session_input_admissions(
                    id,tenant_id,session_id,command_id,input_id,input_revision,state,hook_run_id,
                    hook_idempotency_key,additional_context_refs_json,carry_forward,
                    dispatch_state,dispatch_receipt_json,dispatch_attempts,version
                  ) VALUES (
                    :id,:tenant_id,:session_id,:command_id,:input_id,1,'admitted',:hook_run_id,
                    :hook_key,'[]'::jsonb,'next_admitted_turn','pending','{}'::jsonb,0,1
                  )
                """),
                {
                    "id": admission_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "command_id": command_b,
                    "input_id": input_b,
                    "hook_run_id": uuid.uuid4(),
                    "hook_key": f"binding-hook-{admission_b}",
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_model_results(
                    id,tenant_id,session_id,turn_id,run_id,round_id,provider_request_id,state,
                    model_request_hash,model_request_snapshot_json,bound_input_ids_json
                  ) VALUES (
                    :id,:tenant_id,:session_id,'turn-b',:run_id,'round-b',:provider_request,
                    'prepared',repeat('d',64),'{}'::jsonb,'[]'::jsonb
                  )
                """),
                {
                    "id": result_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "run_id": run_b,
                    "provider_request": f"binding-result-{result_b}",
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_control_inputs(
                    id,tenant_id,session_id,command_id,kind,expected_run_id,
                    authority_snapshot_hash,response_payload_json,response_payload_hash,status
                  ) VALUES (
                    :id,:tenant_id,:session_id,:command_id,'cancel_run',:run_id,
                    repeat('1',64),'{}'::jsonb,repeat('2',64),'accepted'
                  )
                """),
                {
                    "id": control_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "command_id": cancel_command_b,
                    "run_id": run_b,
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_turn_replacements(
                    id,tenant_id,session_id,command_id,old_turn_id,old_run_id,
                    cancel_control_id,cancel_command_id,replacement_turn_id,
                    replacement_input_id,state,generation
                  ) VALUES (
                    :id,:tenant_id,:session_id,:command_id,'turn-b',:run_id,
                    :control_id,:cancel_command_id,'replacement-turn-b',:input_id,
                    'cancel_accepted',1
                  )
                """),
                {
                    "id": replacement_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "command_id": saga_command_b,
                    "run_id": run_b,
                    "control_id": control_b,
                    "cancel_command_id": cancel_command_b,
                    "input_id": input_b,
                },
            )
            await connection.execute(
                text("""
                      INSERT INTO session_tool_invocations(
                        id,tenant_id,session_id,run_id,round_id,provider_request_id,
                        provider_tool_use_id,provider_arguments_json,invocation_item_id,
                        args_hash,authority_snapshot_hash,
                        effect_idempotency_key,effect_state
                      ) VALUES (
                        :id,:tenant_id,:session_id,:run_id,'round-b',:provider_request,
                        :tool_use_id,'{}'::jsonb,:item_id,repeat('e',64),repeat('f',64),:effect_key,
                        'prepared_not_started'
                      )
                """),
                {
                    "id": invocation_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "run_id": run_b,
                    "provider_request": f"binding-invocation-{invocation_b}",
                    "tool_use_id": f"tool-{invocation_b}",
                    "item_id": uuid.uuid4(),
                    "effect_key": f"effect-{invocation_b}",
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_carry_forwards(
                    id,tenant_id,session_id,purpose,source_admission_id,source_input_id,
                    source_hook_run_id,source_evidence_refs_json,context_source_item_id,
                    state,target_turn_id,target_round_id,claim_generation,version
                  )
                  SELECT :id,:tenant_id,:session_id,'prevented_prompt_context',a.id,a.input_id,
                    a.hook_run_id,'[]'::jsonb,:context_item,'round_bound','turn-b','round-b',1,1
                  FROM session_input_admissions a WHERE a.id=:admission_id
                """),
                {
                    "id": carry_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "context_item": uuid.uuid4(),
                    "admission_id": admission_b,
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_round_obligations(
                    id,tenant_id,session_id,turn_id,run_id,source_result_id,kind,
                    source_generation,source_ref,payload_json,state,version
                  ) VALUES (
                    :id,:tenant_id,:session_id,'turn-b',:run_id,:result_id,'tool_followup',
                    1,:source_ref,'{}'::jsonb,'pending',1
                  )
                """),
                {
                    "id": obligation_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "run_id": run_b,
                    "result_id": result_b,
                    "source_ref": f"result:{result_b}",
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_next_round_plans(
                    id,tenant_id,session_id,run_id,source_result_id,next_round_id,
                    obligation_ids_json,ordered_sources_json,fences_json,plan_hash,state,version
                  ) VALUES (
                    :id,:tenant_id,:session_id,:run_id,:result_id,'round-b-next',
                    jsonb_build_array(CAST(:obligation_id AS text)),'[]'::jsonb,'{}'::jsonb,
                    repeat('3',64),'prepared',1
                  )
                """),
                {
                    "id": plan_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "run_id": run_b,
                    "result_id": result_b,
                    "obligation_id": str(obligation_b),
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_run_outcomes(
                    id,tenant_id,session_id,turn_id,run_id,terminal_result_id,state,
                    eligibility_snapshot_hash,version
                  ) VALUES (
                    :id,:tenant_id,:session_id,'turn-b',:run_id,:result_id,'prepared',
                    repeat('4',64),1
                  )
                """),
                {
                    "id": outcome_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "run_id": run_b,
                    "result_id": result_b,
                },
            )
            event_insert_b = text("""
              INSERT INTO chat_transcript_events(
                id,sequence,tenant_id,agent_id,session_id,run_id,schema_version,item_id,
                item_kind,lifecycle,payload_schema,scope_json,result_id,item_type,item_status,
                actor_type,event_type,visibility_scope,listed_surface,content,metadata_json,
                projection_status,projection_attempts
              ) VALUES (
                :id,:sequence,:tenant_id,:agent_id,:session_id,:run_id,2,:item_id,
                :item_kind,:lifecycle,:payload_schema,CAST(:scope AS jsonb),:result_id,
                :item_kind,:lifecycle,:actor_type,:event_type,'operator','ops','',
                CAST(:metadata AS jsonb),'pending',0
              )
            """)
            await connection.execute(
                event_insert_b,
                {
                    "id": target_event_b,
                    "sequence": 1,
                    "tenant_id": tenant_b,
                    "agent_id": agent_b,
                    "session_id": session_b,
                    "run_id": run_b,
                    "item_id": target_item_b,
                    "item_kind": "assistant_text",
                    "lifecycle": "completed",
                    "payload_schema": "hive.session.payload.assistant_text.completed.v2",
                    "scope": (
                        '{"level":"round","session_id":"%s","thread_id":"%s",'
                        '"turn_id":"turn-b","run_id":"%s","round_id":"round-b"}' % (session_b, session_b, run_b)
                    ),
                    "result_id": result_b,
                    "actor_type": "assistant",
                    "event_type": "assistant_text.completed",
                    "metadata": (
                        '{"v2_payload":{"phase":"unknown"},"actor":{"type":"assistant"},'
                        '"visibility":{"audience":"operator"}}'
                    ),
                },
            )
            await connection.execute(
                event_insert_b,
                {
                    "id": mutation_event_b,
                    "sequence": 2,
                    "tenant_id": tenant_b,
                    "agent_id": agent_b,
                    "session_id": session_b,
                    "run_id": None,
                    "item_id": mutation_item_b,
                    "item_kind": "evaluation_feedback_mutation",
                    "lifecycle": "recorded",
                    "payload_schema": ("hive.session.payload.evaluation_feedback_mutation.recorded.v2"),
                    "scope": ('{"level":"session","session_id":"%s","thread_id":"%s"}' % (session_b, session_b)),
                    "result_id": None,
                    "actor_type": "user",
                    "event_type": "evaluation_feedback_mutation.recorded",
                    "metadata": ('{"v2_payload":{},"actor":{"type":"user"},"visibility":{"audience":"operator"}}'),
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_feedback_aggregates(
                    id,tenant_id,session_id,target_item_id,target_result_id,revision,
                    current_value_json,status,last_mutation_item_id
                  ) VALUES (
                    :id,:tenant_id,:session_id,:target_item,:target_result,1,
                    '{"rating":"useful"}'::jsonb,'active',:mutation_item
                  )
                """),
                {
                    "id": feedback_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "target_item": target_item_b,
                    "target_result": result_b,
                    "mutation_item": mutation_item_b,
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_event_outbox(
                    id,tenant_id,session_id,event_id,sequence,envelope_json,envelope_sha256,
                    status,attempts
                  ) VALUES (
                    :id,:tenant_id,:session_id,:event_id,1,'{}'::jsonb,repeat('5',64),
                    'pending',0
                  )
                """),
                {
                    "id": outbox_b,
                    "tenant_id": tenant_b,
                    "session_id": session_b,
                    "event_id": target_event_b,
                },
            )
            await connection.execute(
                text("""
                  INSERT INTO session_event_cursors(session_id,tenant_id,next_sequence,version)
                  VALUES (:session_id,:tenant_id,3,1)
                """),
                {"session_id": session_b, "tenant_id": tenant_b},
            )

        authority_frame_rows = (
            ("session_event_cursors", "session_id", session_b),
            ("session_event_outbox", "id", outbox_b),
            ("session_commands", "id", command_b),
            ("session_turn_inputs", "id", input_b),
            ("session_input_admissions", "id", admission_b),
            ("session_carry_forwards", "id", carry_b),
            ("session_control_inputs", "id", control_b),
            ("session_turn_replacements", "id", replacement_b),
            ("session_tool_invocations", "id", invocation_b),
            ("session_model_results", "id", result_b),
            ("session_round_obligations", "id", obligation_b),
            ("session_next_round_plans", "id", plan_b),
            ("session_run_outcomes", "id", outcome_b),
            ("session_feedback_aggregates", "id", feedback_b),
        )
        for table_name, key_column, row_id in authority_frame_rows:
            await rejected(
                text(f'UPDATE "{table_name}" SET session_id=:peer_session_id WHERE "{key_column}"=:row_id'),
                {"row_id": row_id, "peer_session_id": session_b_peer},
                "session_v2_authority_frame_immutable",
            )

        async with engine.begin() as connection:
            for table_name, key_column, row_id in authority_frame_rows:
                assert (
                    await connection.scalar(
                        text(
                            f'SELECT count(*) FROM "{table_name}" '
                            f'WHERE "{key_column}"=:row_id AND session_id=:session_id'
                        ),
                        {"row_id": row_id, "session_id": session_b},
                    )
                    == 1
                )
            await connection.execute(
                text("UPDATE session_event_cursors SET next_sequence=4,version=2 WHERE session_id=:id"),
                {"id": session_b},
            )
            await connection.execute(
                text("UPDATE session_commands SET status='applied' WHERE id=:id"),
                {"id": command_b},
            )
            await connection.execute(
                text("UPDATE session_input_admissions SET lease_owner='binding-worker',version=version+1 WHERE id=:id"),
                {"id": admission_b},
            )

        cross_session_rows = (
            ("session_event_outbox", outbox_b),
            ("session_commands", saga_command_b),
            ("session_turn_inputs", input_b),
            ("session_input_admissions", admission_b),
            ("session_carry_forwards", carry_b),
            ("session_control_inputs", control_b),
            ("session_turn_replacements", replacement_b),
            ("session_tool_invocations", invocation_b),
            ("session_model_results", result_b),
            ("session_round_obligations", obligation_b),
            ("session_next_round_plans", plan_b),
            ("session_run_outcomes", outcome_b),
            ("session_feedback_aggregates", feedback_b),
        )
        for table_name, row_id in cross_session_rows:
            await rejected(
                text(f'UPDATE "{table_name}" SET tenant_id=:tenant_id,session_id=:session_id WHERE id=:id'),
                {"id": row_id, "tenant_id": tenant_a, "session_id": session_a},
                "session_v2_authority_frame_immutable",
            )

        await rejected(
            text("""
              INSERT INTO session_event_cursors(session_id,tenant_id,next_sequence,version)
              VALUES (:session_id,:tenant_id,1,1)
            """),
            {"session_id": session_a, "tenant_id": tenant_b},
            "session_v2_tenant_binding_mismatch",
        )
        await rejected(
            text("""
              INSERT INTO session_commands(
                id,tenant_id,principal_id,session_id,namespace,idempotency_key,command_kind,
                request_hash,target_hash,request_json,target_json,status,receipt_ref
              ) VALUES (
                :id,:tenant_id,:principal_id,:session_id,'human_input',:key,'start_turn',
                repeat('a',64),repeat('b',64),'{}'::jsonb,'{}'::jsonb,'accepted','fixture'
              )
            """),
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "principal_id": user_b,
                "session_id": session_a,
                "key": f"binding-{uuid.uuid4()}",
            },
            "session_v2_tenant_binding_mismatch",
        )

        legacy_insert = text("""
          INSERT INTO chat_transcript_events(
            id,sequence,tenant_id,agent_id,session_id,schema_version,item_type,item_status,
            actor_type,event_type,visibility_scope,listed_surface,content,metadata_json,
            projection_status,projection_attempts
          ) VALUES (
            :id,1,:tenant_id,:agent_id,:session_id,1,'event','succeeded','system',
            'binding_fixture','operator','ops','fixture','{}'::jsonb,'not_requested',0
          )
        """)
        await rejected(
            legacy_insert,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "agent_id": agent_a,
                "session_id": session_a,
            },
            "session_event_authority_binding_mismatch",
        )

        v2_insert = text("""
          INSERT INTO chat_transcript_events(
            id,sequence,tenant_id,agent_id,session_id,run_id,schema_version,item_id,item_kind,
            lifecycle,payload_schema,scope_json,command_id,input_id,result_id,invocation_id,
            item_type,item_status,actor_type,event_type,visibility_scope,listed_surface,content,
            metadata_json,projection_status,projection_attempts
          ) VALUES (
            :id,1,:tenant_id,:agent_id,:session_id,:run_id,2,:item_id,:item_kind,
            :lifecycle,:payload_schema,CAST(:scope_json AS jsonb),:command_id,:input_id,
            :result_id,:invocation_id,:item_kind,:lifecycle,:actor_type,:event_type,
            'direct_user','chat','',CAST(:metadata_json AS jsonb),'pending',0
          )
        """)
        round_scope_a = (
            '{"level":"round","session_id":"%s","thread_id":"%s",'
            '"turn_id":"turn-a","run_id":"%s","round_id":"round-a"}' % (session_a, session_a, run_a)
        )
        base_event = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_a,
            "agent_id": agent_a,
            "session_id": session_a,
            "run_id": run_a,
            "item_id": uuid.uuid4(),
            "item_kind": "assistant_text",
            "lifecycle": "completed",
            "payload_schema": "hive.session.payload.assistant_text.completed.v2",
            "scope_json": round_scope_a,
            "command_id": None,
            "input_id": None,
            "result_id": None,
            "invocation_id": None,
            "actor_type": "assistant",
            "event_type": "assistant_text.completed",
            "metadata_json": (
                '{"v2_payload":{"phase":"unknown"},"actor":{"type":"assistant"},'
                '"visibility":{"audience":"direct_user"}}'
            ),
        }

        async def reject_v2(expected: str, **overrides) -> None:
            values = {**base_event, **overrides, "id": uuid.uuid4(), "item_id": uuid.uuid4()}
            await rejected(v2_insert, values, expected)

        await reject_v2(
            "session_event_run_scope_mismatch",
            scope_json=round_scope_a.replace(str(run_a), str(run_b)),
        )
        await reject_v2(
            "session_event_run_scope_mismatch",
            item_kind="runtime_failure",
            lifecycle="recorded",
            payload_schema="hive.session.payload.runtime_failure.recorded.v2",
            event_type="runtime_failure.recorded",
            actor_type="runtime",
            scope_json=('{"level":"session","session_id":"%s","thread_id":"%s"}' % (session_a, session_a)),
            metadata_json=('{"v2_payload":{},"actor":{"type":"runtime"},"visibility":{"audience":"operator"}}'),
        )
        await reject_v2(
            "session_event_run_authority_mismatch",
            run_id=run_b,
            scope_json=round_scope_a.replace(str(run_a), str(run_b)),
        )
        await reject_v2("session_event_command_authority_mismatch", command_id=command_b)
        await reject_v2("session_event_input_authority_mismatch", input_id=input_b)
        await reject_v2("session_event_result_authority_mismatch", result_id=result_b)
        await reject_v2("session_event_invocation_authority_mismatch", invocation_id=invocation_b)
        await rejected(
            legacy_insert,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_a,
                "agent_id": agent_b,
                "session_id": session_a,
            },
            "session_event_authority_binding_mismatch",
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_writer_epoch_db_fence_rejects_late_v1_and_old_generation_mutations(
    migrated_pg_url,
) -> None:
    """Exercise the rolling N/N+1 fence entirely through PostgreSQL triggers."""

    from sqlalchemy.exc import DBAPIError

    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User

    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    (
        tenant_id,
        user_id,
        agent_id,
        session_id,
        run_v1,
        run_v2,
        other_tenant_id,
        wrong_tenant_run,
        wrong_session_run,
        wrong_agent_run,
    ) = (uuid.uuid4() for _ in range(10))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    runtime_insert = text("""
      INSERT INTO runtime_tasks(
        id,task_type,tenant_id,status,writer_generation,delegation_chain_json,depth,
        priority,attempt_count,claim_version,root_idempotency_key,config_snapshot_hash,
        policy_snapshot_hash
      ) VALUES (
        :id,'delegation',:tenant_id,'running',:writer_generation,'[]'::jsonb,1,
        0,0,0,:root_key,repeat('a',64),repeat('b',64)
      )
    """)
    event_insert = text("""
      INSERT INTO chat_transcript_events(
        id,sequence,tenant_id,agent_id,session_id,run_id,schema_version,
        item_id,item_kind,lifecycle,payload_schema,scope_json,item_type,item_status,
        actor_type,event_type,visibility_scope,listed_surface,content,metadata_json,
        projection_status,projection_attempts
      ) VALUES (
        :id,:sequence,:tenant_id,:agent_id,:session_id,:run_id,:schema_version,
        :item_id,:item_kind,:lifecycle,:payload_schema,CAST(:scope_json AS jsonb),
        :item_type,:item_status,:actor_type,:event_type,'operator','ops','fixture',
        CAST(:metadata_json AS jsonb),'not_requested',0
      )
    """)

    async def set_epoch(*, state: str, new_generation: int, allowed: str) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                  UPDATE session_writer_epochs
                  SET state=:state,new_run_generation=:new_generation,
                      allowed_existing_generations_json=CAST(:allowed AS jsonb),
                      enforcement_mode='enforce',version=version+1,updated_at=now()
                  WHERE id='global'
                """),
                {"state": state, "new_generation": new_generation, "allowed": allowed},
            )

    async def execute_ok(statement, values) -> None:
        async with engine.begin() as connection:
            await connection.execute(statement, values)

    async def execute_rejected(statement, values, message: str) -> None:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(DBAPIError, match=message):
                await connection.execute(statement, values)
            await transaction.rollback()

    def event_values(
        *,
        sequence: int,
        schema_version: int,
        run_id: uuid.UUID | None,
    ) -> dict[str, object]:
        if schema_version == 1:
            return {
                "id": uuid.uuid4(),
                "sequence": sequence,
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "run_id": run_id,
                "schema_version": 1,
                "item_id": None,
                "item_kind": None,
                "lifecycle": None,
                "payload_schema": None,
                "scope_json": None,
                "item_type": "event",
                "item_status": "succeeded",
                "actor_type": "system",
                "event_type": "legacy_epoch_fixture",
                "metadata_json": "{}",
            }
        assert run_id is not None
        return {
            "id": uuid.uuid4(),
            "sequence": sequence,
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "run_id": run_id,
            "schema_version": 2,
            "item_id": uuid.uuid4(),
            "item_kind": "runtime_failure",
            "lifecycle": "recorded",
            "payload_schema": "hive.session.payload.runtime_failure.recorded.v2",
            "scope_json": (
                '{"level":"run","session_id":"%s","thread_id":"%s",'
                '"turn_id":"turn-epoch","run_id":"%s"}' % (session_id, session_id, run_id)
            ),
            "item_type": "runtime_failure",
            "item_status": "recorded",
            "actor_type": "runtime",
            "event_type": "runtime_failure.recorded",
            "metadata_json": (
                '{"v2_payload":{"domain":"runtime","code":"epoch_fixture"},'
                '"actor":{"type":"runtime"},"visibility":{"audience":"operator"}}'
            ),
        }

    try:
        async with session_factory() as db:
            await db.execute(
                text("""
                  UPDATE session_writer_epochs
                  SET state='legacy_open',new_run_generation=1,
                      allowed_existing_generations_json='[1]'::jsonb,
                      enforcement_mode='observe',version=version+1
                  WHERE id='global'
                """)
            )
            db.add_all(
                [
                    Tenant(id=tenant_id, name="Epoch Fence", slug=f"epoch-{tenant_id.hex[:8]}"),
                    Tenant(
                        id=other_tenant_id,
                        name="Epoch Fence Other",
                        slug=f"epoch-other-{other_tenant_id.hex[:8]}",
                    ),
                ]
            )
            db.add(
                User(
                    id=user_id,
                    username=f"epoch-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@epoch.test",
                    password_hash="x",
                    display_name="Epoch",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Epoch Agent", creator_id=user_id))
            await db.flush()
            db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
            db.add_all(
                [
                    RuntimeTask(
                        id=run_v1,
                        tenant_id=tenant_id,
                        task_type="delegation",
                        parent_agent_id=agent_id,
                        parent_session_id=str(session_id),
                        status="running",
                        writer_generation=1,
                    ),
                    RuntimeTask(
                        id=run_v2,
                        tenant_id=tenant_id,
                        task_type="delegation",
                        parent_agent_id=agent_id,
                        parent_session_id=str(session_id),
                        status="running",
                        writer_generation=2,
                    ),
                    RuntimeTask(
                        id=wrong_tenant_run,
                        tenant_id=other_tenant_id,
                        task_type="delegation",
                        parent_agent_id=agent_id,
                        parent_session_id=str(session_id),
                        status="running",
                        writer_generation=1,
                    ),
                    RuntimeTask(
                        id=wrong_session_run,
                        tenant_id=tenant_id,
                        task_type="delegation",
                        parent_agent_id=agent_id,
                        parent_session_id=str(uuid.uuid4()),
                        status="running",
                        writer_generation=1,
                    ),
                    RuntimeTask(
                        id=wrong_agent_run,
                        tenant_id=tenant_id,
                        task_type="delegation",
                        parent_agent_id=uuid.uuid4(),
                        parent_session_id=str(session_id),
                        status="running",
                        writer_generation=1,
                    ),
                ]
            )
            await db.commit()

        await set_epoch(state="v1_draining", new_generation=2, allowed="[1,2]")

        for foreign_run_id in (wrong_tenant_run, wrong_session_run, wrong_agent_run):
            await execute_rejected(
                event_insert,
                event_values(sequence=1, schema_version=1, run_id=foreign_run_id),
                "legacy run authority",
            )

        # Existing generation-1 Run may drain; a new generation-1 Run may not start.
        await execute_ok(
            text("UPDATE runtime_tasks SET metadata_json='{}'::jsonb WHERE id=:id"),
            {"id": run_v1},
        )
        await execute_rejected(
            runtime_insert,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "writer_generation": 1,
                "root_key": f"epoch-gen1-{uuid.uuid4()}",
            },
            "writer_epoch_rejected new run generation",
        )
        await execute_ok(
            runtime_insert,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "writer_generation": 2,
                "root_key": f"epoch-gen2-{uuid.uuid4()}",
            },
        )

        legacy_v1 = event_values(sequence=1, schema_version=1, run_id=run_v1)
        await execute_ok(event_insert, legacy_v1)
        await execute_rejected(
            event_insert,
            event_values(sequence=2, schema_version=1, run_id=None),
            "writer_epoch_rejected legacy transcript mutation",
        )
        await execute_rejected(
            event_insert,
            event_values(sequence=2, schema_version=1, run_id=run_v2),
            "writer_epoch_rejected legacy run generation",
        )
        await execute_ok(event_insert, event_values(sequence=2, schema_version=2, run_id=run_v2))
        await execute_rejected(
            event_insert,
            event_values(sequence=3, schema_version=2, run_id=run_v1),
            "writer_epoch_rejected V2 run generation",
        )

        await set_epoch(state="v2_only", new_generation=2, allowed="[2]")

        await execute_rejected(
            text("UPDATE runtime_tasks SET metadata_json='{}'::jsonb WHERE id=:id"),
            {"id": run_v1},
            "writer_epoch_rejected late runtime mutation",
        )
        await execute_ok(
            text("UPDATE runtime_tasks SET metadata_json='{}'::jsonb WHERE id=:id"),
            {"id": run_v2},
        )
        await execute_rejected(
            text("UPDATE chat_transcript_events SET content='late-v1' WHERE id=:id"),
            {"id": legacy_v1["id"]},
            "writer_epoch_rejected legacy transcript mutation",
        )
        await execute_rejected(
            event_insert,
            event_values(sequence=3, schema_version=1, run_id=run_v1),
            "writer_epoch_rejected legacy transcript mutation",
        )
        await execute_rejected(
            event_insert,
            event_values(sequence=3, schema_version=2, run_id=run_v1),
            "writer_epoch_rejected V2 run generation",
        )
        await execute_ok(event_insert, event_values(sequence=3, schema_version=2, run_id=run_v2))

        # T0 projection is a derived sidecar transition performed by the
        # current runtime, not a late legacy semantic writer.  A pre-existing
        # row must remain projectable after its generation has drained from
        # the semantic writer epoch.
        await execute_ok(
            text("""
              UPDATE chat_transcript_events
              SET projection_status='projected',projection_attempts=1,
                  projected_at=now(),
                  metadata_json=metadata_json ||
                    jsonb_build_object(
                      't0_bridge_pending',false,
                      't0_bridge_relay_source','runtime_control_bus'
                    )
              WHERE id=:id
            """),
            {"id": legacy_v1["id"]},
        )
        await execute_rejected(
            text("""
              UPDATE chat_transcript_events
              SET metadata_json=metadata_json || jsonb_build_object('semantic_rewrite',true)
              WHERE id=:id
            """),
            {"id": legacy_v1["id"]},
            "writer_epoch_rejected legacy transcript mutation",
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                  UPDATE session_writer_epochs
                  SET state='legacy_open',new_run_generation=1,
                      allowed_existing_generations_json='[1]'::jsonb,
                      enforcement_mode='observe',version=version+1
                  WHERE id='global'
                """)
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_delete_fk_index_revision_recovers_invalid_residue_and_validates_all_five_indexes(
    session_v2_invalid_index_pg_url,
) -> None:
    """Invalid concurrent-build residue is dropped/rebuilt, and post-upgrade
    validation proves every one of the five delete-FK indexes valid/ready."""

    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    _alembic_downgrade(session_v2_invalid_index_pg_url, "invitation_role_binding_0831")

    engine = create_async_engine(session_v2_invalid_index_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            # Plant failed-concurrent-build residue for the single index the
            # observed production timeout was proven to depend on: the index
            # exists but is not valid, which a naive
            # CREATE INDEX CONCURRENTLY IF NOT EXISTS would silently keep.
            await autocommit.execute(
                text(
                    "CREATE INDEX ix_chat_transcript_events_parent_event_id "
                    "ON public.chat_transcript_events (parent_event_id)"
                )
            )
            residue = (
                await autocommit.execute(
                    text(
                        "UPDATE pg_index SET indisvalid=false "
                        "WHERE indexrelid='public.ix_chat_transcript_events_parent_event_id'::regclass "
                        "RETURNING indexrelid"
                    )
                )
            ).scalar()
            assert residue is not None
    finally:
        await engine.dispose()

    _alembic_upgrade(session_v2_invalid_index_pg_url, "head")

    verified = create_async_engine(session_v2_invalid_index_pg_url, poolclass=NullPool)
    try:
        async with verified.connect() as connection:
            version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == "session_v2_delete_fk_indexes_0907"
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT index_class.relname,index_row.indisvalid,index_row.indisready
                        FROM pg_catalog.pg_index AS index_row
                        JOIN pg_catalog.pg_class AS index_class
                          ON index_class.oid=index_row.indexrelid
                        JOIN pg_catalog.pg_namespace AS namespace
                          ON namespace.oid=index_class.relnamespace
                        WHERE namespace.nspname='public'
                          AND index_class.relname=ANY(:index_names)
                        ORDER BY index_class.relname
                        """,
                    ),
                    {
                        "index_names": [
                            "ix_chat_transcript_events_parent_event_id",
                            "ix_session_carry_forwards_consumed_event_id",
                            "ix_session_turn_replacements_last_event_id",
                            "ix_session_model_results_round_committed_event_id",
                            "ix_session_run_outcomes_terminal_event_id",
                        ]
                    },
                )
            ).all()
            assert len(rows) == 5
            for row in rows:
                assert row.indisvalid is True, row.relname
                assert row.indisready is True, row.relname
    finally:
        await verified.dispose()
