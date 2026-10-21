"""Add the HTTP API's request log and feedback, and the API key of chat spending

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_spend", sa.Column("api_key", sa.String(length=32), nullable=True))
    op.create_index(
        "ix_llm_spend_api_key_recorded_at", "llm_spend", ["api_key", "recorded_at"], unique=False
    )
    op.create_table(
        "request_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=True),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("tool_calls", sa.SmallInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_request_log_agent_run_id_agent_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request_log")),
        sa.UniqueConstraint("request_id", name=op.f("uq_request_log_request_id")),
    )
    op.create_index("ix_request_log_started_at", "request_log", ["started_at"], unique=False)
    op.create_index(
        "ix_request_log_key_id_started_at", "request_log", ["key_id", "started_at"], unique=False
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column("rating", sa.String(length=4), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("key_id", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("rating IN ('up', 'down')", name=op.f("ck_feedback_rating")),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["request_log.request_id"],
            name=op.f("fk_feedback_request_id_request_log"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    op.create_index("ix_feedback_request_id", "feedback", ["request_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_feedback_request_id", table_name="feedback")
    op.drop_table("feedback")
    op.drop_index("ix_request_log_key_id_started_at", table_name="request_log")
    op.drop_index("ix_request_log_started_at", table_name="request_log")
    op.drop_table("request_log")
    op.drop_index("ix_llm_spend_api_key_recorded_at", table_name="llm_spend")
    op.drop_column("llm_spend", "api_key")
