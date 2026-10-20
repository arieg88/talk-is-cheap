"""Add agent runs and the agent's checkpointer tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19

The checkpointer tables are langgraph-checkpoint-postgres's own schema (3.2.0), created
here the way its `setup()` creates them, so `make migrate` sets up everything and tests
get them in their own database. Its migration numbers are recorded in
`checkpoint_migrations`, so the library sees them as applied
(`agent/memory.py: check_checkpointer_schema` compares them with the installed
version). The library creates its indexes CONCURRENTLY, which cannot run inside a
migration's transaction; the tables are new and empty here, so they are created plainly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# langgraph-checkpoint-postgres 3.2.0, langgraph/checkpoint/postgres/base.py: MIGRATIONS,
# position = version.
CHECKPOINT_MIGRATIONS = [
    "CREATE TABLE IF NOT EXISTS checkpoint_migrations (v INTEGER PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS checkpoints (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        parent_checkpoint_id TEXT,
        type TEXT,
        checkpoint JSONB NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoint_blobs (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL,
        version TEXT NOT NULL,
        type TEXT NOT NULL,
        blob BYTEA,
        PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoint_writes (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        channel TEXT NOT NULL,
        type TEXT,
        blob BYTEA NOT NULL,
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
    )""",
    "ALTER TABLE checkpoint_blobs ALTER COLUMN blob DROP NOT NULL",
    "SELECT 1",
    "CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints(thread_id)",
    "CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id)",
    "CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id)",
    "ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT ''",
]


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("question_type", sa.String(length=16), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("cached_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("seconds", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("revisions", sa.SmallInteger(), nullable=False),
        sa.Column("verifier_failures", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index("ix_agent_runs_started_at", "agent_runs", ["started_at"], unique=False)
    op.create_index("ix_agent_runs_thread_id", "agent_runs", ["thread_id"], unique=False)

    for version, statement in enumerate(CHECKPOINT_MIGRATIONS):
        op.execute(statement)
        op.execute(f"INSERT INTO checkpoint_migrations (v) VALUES ({version})")


def downgrade() -> None:
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.drop_index("ix_agent_runs_thread_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_started_at", table_name="agent_runs")
    op.drop_table("agent_runs")
