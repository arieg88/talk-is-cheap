"""Add progress to archive searches: pages found and done, and whether a search is complete

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("archive_searches", sa.Column("pages", sa.Integer(), nullable=True))
    op.add_column(
        "archive_searches",
        sa.Column("pages_done", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "archive_searches",
        sa.Column("complete", sa.Boolean(), server_default="true", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("archive_searches", "complete")
    op.drop_column("archive_searches", "pages_done")
    op.drop_column("archive_searches", "pages")
