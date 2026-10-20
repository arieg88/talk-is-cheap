"""Add related companies and comparison basis to promises, and findings to verdicts

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "promise_verdicts",
        sa.Column(
            "assessments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "promises",
        sa.Column(
            "related_companies",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column("promises", sa.Column("comparison_basis", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        op.f("ck_promises_comparison_basis"),
        "promises",
        "comparison_basis IS NULL OR comparison_basis IN ('yoy', 'sequential', 'absolute')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_promises_comparison_basis"), "promises", type_="check")
    op.drop_column("promises", "comparison_basis")
    op.drop_column("promises", "related_companies")
    op.drop_column("promise_verdicts", "assessments")
