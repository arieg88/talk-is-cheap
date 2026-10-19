"""Add the LLM spend ledger, and the post-processing version of guidance extractions

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_spend",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("command", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("counted", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_spend")),
    )
    op.create_index("ix_llm_spend_recorded_at", "llm_spend", ["recorded_at"], unique=False)
    # Extractions stored before post-processing was versioned were made with version 1.
    op.add_column(
        "guidance_extractions",
        sa.Column("processing_version", sa.String(length=16), server_default="1", nullable=False),
    )
    op.alter_column("guidance_extractions", "processing_version", server_default=None)


def downgrade() -> None:
    op.drop_column("guidance_extractions", "processing_version")
    op.drop_index("ix_llm_spend_recorded_at", table_name="llm_spend")
    op.drop_table("llm_spend")
