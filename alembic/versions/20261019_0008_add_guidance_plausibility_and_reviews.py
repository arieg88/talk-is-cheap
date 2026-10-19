"""Add plausibility flags and item keys to guidance, and review decisions

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("guidance", sa.Column("plausibility", sa.String(length=16), nullable=True))
    op.add_column("guidance", sa.Column("plausibility_rule", sa.String(length=32), nullable=True))
    op.add_column("guidance", sa.Column("plausibility_note", sa.Text(), nullable=True))
    op.add_column("guidance", sa.Column("review", sa.String(length=8), nullable=True))
    # Existing rows get their key from the next `extract guidance`, which refreshes every
    # row (guidance/matching.py: item_key).
    op.add_column(
        "guidance",
        sa.Column("item_key", sa.String(length=64), server_default="", nullable=False),
    )
    op.alter_column("guidance", "item_key", server_default=None)
    op.create_check_constraint(
        "plausibility",
        "guidance",
        "plausibility IS NULL OR plausibility IN ('passed', 'flagged', 'skipped')",
    )
    op.create_check_constraint("review", "guidance", "review IS NULL OR review IN ('ok', 'wrong')")
    op.create_table(
        "guidance_reviews",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("accession_no", sa.String(length=20), nullable=False),
        sa.Column("item_key", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=8), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('ok', 'wrong')", name=op.f("ck_guidance_reviews_decision")
        ),
        sa.ForeignKeyConstraint(
            ["cik"],
            ["companies.cik"],
            name=op.f("fk_guidance_reviews_cik_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guidance_reviews")),
        sa.UniqueConstraint("accession_no", "item_key", name="uq_guidance_reviews_item"),
    )


def downgrade() -> None:
    op.drop_table("guidance_reviews")
    op.drop_constraint(op.f("ck_guidance_review"), "guidance", type_="check")
    op.drop_constraint(op.f("ck_guidance_plausibility"), "guidance", type_="check")
    op.drop_column("guidance", "item_key")
    op.drop_column("guidance", "review")
    op.drop_column("guidance", "plausibility_note")
    op.drop_column("guidance", "plausibility_rule")
    op.drop_column("guidance", "plausibility")
