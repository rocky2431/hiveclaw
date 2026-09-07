from __future__ import annotations

import importlib.util
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "merge_incident_kimi_0725.py"
INCIDENT_HEAD = "completion_outbox_index_0721"
KIMI_HEAD = "retire_agent_agent_relationships_table_0724"
MERGE_HEAD = "merge_incident_kimi_0725"
# The merge revision itself is immutable history; the closure head moves with
# every newer revision (currently the A2A continuation task contract).
CURRENT_HEAD = "session_v2_delete_fk_indexes_0907"
BRANCH_POINT = "im_unverified_transport_0719"


def _module():
    spec = importlib.util.spec_from_file_location(MERGE_HEAD, MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_incident_and_kimi_heads_merge_without_rewriting_history() -> None:
    module = _module()
    source = MIGRATION.read_text(encoding="utf-8")

    assert module.revision == MERGE_HEAD
    assert module.down_revision == (INCIDENT_HEAD, KIMI_HEAD)
    assert "production before" in source
    assert "def upgrade() -> None:\n    pass" in source
    assert "def downgrade() -> None:\n    pass" in source


async def _branch_snapshot(database_url: str) -> dict[str, object]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return {
                "version": await connection.scalar(text("SELECT version_num FROM alembic_version")),
                "completion_column": await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='runtime_tasks' "
                        "AND column_name='completion_outbox_generation'"
                    )
                ),
                "completion_index": await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_index AS index_row "
                        "JOIN pg_catalog.pg_class AS index_class "
                        "ON index_class.oid=index_row.indexrelid "
                        "WHERE index_class.relname='ix_runtime_tasks_completion_outbox_pending' "
                        "AND index_row.indisvalid AND index_row.indisready"
                    )
                ),
                "company_knowledge_table": await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name='company_knowledge_sources'"
                    )
                ),
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("branch_head", "branch_completion", "branch_company_knowledge"),
    (
        (INCIDENT_HEAD, 1, 0),
        (KIMI_HEAD, 0, 1),
    ),
)
async def test_merge_upgrades_safely_from_either_real_branch_head(
    pg_container,
    branch_head: str,
    branch_completion: int,
    branch_company_knowledge: int,
) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_downgrade, _alembic_upgrade

    database_name = f"merge_branch_{uuid.uuid4().hex[:12]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    try:
        # Empty databases use the production bootstrap shortcut and are stamped
        # directly at the current head. Rewind that empty schema through the
        # real downgrade contracts to the shared branch point before replaying
        # exactly one of the two historical branches.
        _alembic_upgrade(database_url, "head")
        _alembic_downgrade(database_url, BRANCH_POINT)
        _alembic_upgrade(database_url, branch_head)
        assert await _branch_snapshot(database_url) == {
            "version": branch_head,
            "completion_column": branch_completion,
            "completion_index": branch_completion,
            "company_knowledge_table": branch_company_knowledge,
        }

        _alembic_upgrade(database_url, "head")
        assert await _branch_snapshot(database_url) == {
            "version": CURRENT_HEAD,
            "completion_column": 1,
            "completion_index": 1,
            "company_knowledge_table": 1,
        }
    finally:
        code, output = pg_container.exec(
            [
                "psql",
                "-U",
                "test",
                "-d",
                "postgres",
                "-c",
                f"DROP DATABASE IF EXISTS {database_name} WITH (FORCE)",
            ]
        )
        assert code == 0, output
