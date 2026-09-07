"""Index inbound chat_transcript_events foreign keys for Session deletion.

SESSION-V2-DELETE-ORDER-001: the supported ``DELETE
/api/v1/chat/{agent_id}/sessions/{session_id}`` route timed out in production
(asyncpg QueryCanceledError after 30.080s) inside ``DELETE FROM
chat_transcript_events WHERE session_id = ...``. Root cause: PostgreSQL fires
one referential-integrity probe per deleted row for every inbound foreign
key, and the referencing columns below had no supporting index, so each
probe was a sequential scan of a large table (1,137 deleted rows x multiple
full scans > statement timeout). Real-PostgreSQL evidence:
``ix_chat_transcript_events_parent_event_id`` is individually proven
necessary and sufficient for the observed timeout (dropping only that one
index reproduces the statement-timeout cancel; keeping only it removes the
timeout). The other four indexes are the same evidenced FK-support pattern
for the remaining unindexed inbound transcript foreign keys on Session V2
tables that grow per turn; the current performance test did not prove each
of them individually necessary.

Residue recovery reuses the ``session_v2_0716`` concurrent-index contract:
before each create, a pre-existing index that is not both ``indisvalid`` and
``indisready`` (failed concurrent-build residue that ``IF NOT EXISTS``
would silently keep) is dropped concurrently and rebuilt; after the create,
the index must exist and be valid/ready or the migration fails loudly
instead of stamping success over a still-invalid index.

Indexes are additive query-performance structures: no constraint, policy,
or cleanup-contract semantics change.

Revision ID: session_v2_delete_fk_indexes_0907
Revises: invitation_role_binding_0831
Create Date: 2026-09-07
"""

from alembic import op
from sqlalchemy import text


revision = "session_v2_delete_fk_indexes_0907"
down_revision = "invitation_role_binding_0831"
branch_labels = None
depends_on = None


# (table, column) pairs of inbound NO ACTION/CASCADE foreign keys into
# chat_transcript_events.id that had no supporting index.
# session_event_outbox.event_id and session_tool_invocations.result_event_id
# are already covered by UNIQUE constraints.
_FK_INDEXES = (
    ("chat_transcript_events", "parent_event_id"),
    ("session_carry_forwards", "consumed_event_id"),
    ("session_turn_replacements", "last_event_id"),
    ("session_model_results", "round_committed_event_id"),
    ("session_run_outcomes", "terminal_event_id"),
)

_INDEX_STATE_SQL = text(
    """
    SELECT index_row.indisvalid,index_row.indisready
    FROM pg_catalog.pg_index AS index_row
    JOIN pg_catalog.pg_class AS index_class
      ON index_class.oid=index_row.indexrelid
    JOIN pg_catalog.pg_namespace AS index_namespace
      ON index_namespace.oid=index_class.relnamespace
    JOIN pg_catalog.pg_class AS table_class
      ON table_class.oid=index_row.indrelid
    JOIN pg_catalog.pg_namespace AS table_namespace
      ON table_namespace.oid=table_class.relnamespace
    WHERE index_namespace.nspname='public'
      AND table_namespace.nspname='public'
      AND table_class.relname=:table_name
      AND index_class.relname=:index_name
    """
)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _index_state(table_name: str, index_name: str):
    return (
        op.get_bind()
        .execute(_INDEX_STATE_SQL, {"table_name": table_name, "index_name": index_name})
        .mappings()
        .one_or_none()
    )


def _ensure_fk_indexes() -> None:
    """Recover invalid concurrent-build residue without rebuilding healthy siblings."""

    with op.get_context().autocommit_block():
        for table, column in _FK_INDEXES:
            index_name = f"ix_{table}_{column}"
            qualified = f"{_quote_identifier('public')}.{_quote_identifier(index_name)}"
            state = _index_state(table, index_name)
            if state is not None and (not state["indisvalid"] or not state["indisready"]):
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {qualified}")
            op.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" ON public."{table}" ("{column}")')
            rebuilt_state = _index_state(table, index_name)
            if rebuilt_state is None or not rebuilt_state["indisvalid"] or not rebuilt_state["indisready"]:
                raise RuntimeError(f"session_v2_delete_fk_index_rebuild_failed: {qualified}")


def upgrade() -> None:
    _ensure_fk_indexes()


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for table, column in _FK_INDEXES:
            index_name = f"ix_{table}_{column}"
            qualified = f"{_quote_identifier('public')}.{_quote_identifier(index_name)}"
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {qualified}")
