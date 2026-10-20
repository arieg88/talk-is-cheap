"""Add claim types, guidance links, event dates, revisions and expiry to promises

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

VERDICTS = (
    "'delivered', 'delivered_late', 'partly_delivered', 'not_delivered', 'walked_back', "
    "'too_early', 'cannot_determine'"
)
WITH_EXPIRED = VERDICTS.replace("'too_early', ", "'too_early', 'expired_unproven', ")
# Tables and columns holding a promise verdict.
VERDICT_COLUMNS = (
    ("promise_verdicts", "verdict"),
    ("promise_verdicts", "model_verdict"),
    ("promise_reviews", "verdict"),
)


def _verdicts(values: str) -> None:
    for table, column in VERDICT_COLUMNS:
        name = op.f(f"ck_{table}_{column}")
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"{column} IN ({values})")


revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "promise_verdicts", sa.Column("proof_date_source", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "promise_verdicts",
        sa.Column("revised_before_due", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "promises",
        sa.Column("claim_type", sa.String(length=16), server_default="promise", nullable=False),
    )
    op.add_column("promises", sa.Column("guidance_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_promises_guidance_id_guidance"),
        "promises",
        "guidance",
        ["guidance_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_promises_claim_type"), "promises", "claim_type IN ('promise', 'scheduled_action')"
    )
    _verdicts(WITH_EXPIRED)


def downgrade() -> None:
    _verdicts(VERDICTS)
    op.drop_constraint(op.f("ck_promises_claim_type"), "promises", type_="check")
    op.drop_constraint(op.f("fk_promises_guidance_id_guidance"), "promises", type_="foreignkey")
    op.drop_column("promises", "guidance_id")
    op.drop_column("promises", "claim_type")
    op.drop_column("promise_verdicts", "revised_before_due")
    op.drop_column("promise_verdicts", "proof_date_source")
