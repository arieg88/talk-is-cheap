"""Add promises, their verdicts and reviews

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "promise_reviews",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("claim_key", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verdict IN ('delivered', 'delivered_late', 'partly_delivered', 'not_delivered', 'walked_back', 'too_early', 'cannot_determine')",
            name=op.f("ck_promise_reviews_verdict"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promise_reviews")),
        sa.UniqueConstraint("claim_key", name="uq_promise_reviews_claim"),
    )
    op.create_table(
        "promises",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("claim_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.Column("statement_id", sa.BigInteger(), nullable=True),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("speaker", sa.Text(), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=True),
        sa.Column("measure_period", sa.String(length=8), nullable=True),
        sa.Column("target_kind", sa.String(length=16), nullable=True),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("target_value", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("target_text", sa.Text(), nullable=True),
        sa.Column("required_value", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("event", sa.Text(), nullable=True),
        sa.Column("baseline_value", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("baseline_period", sa.String(length=32), nullable=True),
        sa.Column("baseline_source", sa.String(length=16), nullable=True),
        sa.Column("baseline_accession_no", sa.String(length=20), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("due_basis", sa.String(length=16), nullable=False),
        sa.Column("deadline_text", sa.Text(), nullable=True),
        sa.Column("resolution_rule", sa.Text(), nullable=False),
        sa.Column("specificity", sa.String(length=8), nullable=False),
        sa.Column("bold", sa.Boolean(), nullable=False),
        sa.Column("bold_basis", sa.Text(), nullable=True),
        sa.Column("group_key", sa.String(length=64), nullable=False),
        sa.Column("framing", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "framed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "baseline_source IS NULL OR baseline_source IN ('article', 'actuals', 'unknown')",
            name=op.f("ck_promises_baseline_source"),
        ),
        sa.CheckConstraint(
            "direction IN ('at_least', 'at_most')", name=op.f("ck_promises_direction")
        ),
        sa.CheckConstraint(
            "due_basis IN ('explicit', 'relative', 'fiscal_period', 'open_ended')",
            name=op.f("ck_promises_due_basis"),
        ),
        sa.CheckConstraint(
            "specificity IN ('specific', 'partial', 'vague')", name=op.f("ck_promises_specificity")
        ),
        sa.CheckConstraint(
            "target_kind IS NULL OR target_kind IN ('multiple', 'absolute', 'change', 'threshold', 'event')",
            name=op.f("ck_promises_target_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_promises_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["statements.id"],
            name=op.f("fk_promises_statement_id_statements"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promises")),
        sa.UniqueConstraint("claim_key", "version", name="uq_promises_claim_version"),
    )
    op.create_index("ix_promises_group_key", "promises", ["group_key"], unique=False)
    op.create_index("ix_promises_ticker_current", "promises", ["ticker", "current"], unique=False)
    op.create_table(
        "promise_verdicts",
        sa.Column("promise_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("model_verdict", sa.String(length=20), nullable=False),
        sa.Column("failed_checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("measured_value", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("measured_unit", sa.String(length=32), nullable=True),
        sa.Column("measured", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("gathered", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("proof_date", sa.Date(), nullable=True),
        sa.Column("grace_end", sa.Date(), nullable=True),
        sa.Column("days_late", sa.Integer(), nullable=True),
        sa.Column("rule_version", sa.String(length=32), nullable=False),
        sa.Column("rule_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_key", sa.String(length=64), nullable=False),
        sa.Column("resolved_with", sa.BigInteger(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "model_verdict IN ('delivered', 'delivered_late', 'partly_delivered', 'not_delivered', 'walked_back', 'too_early', 'cannot_determine')",
            name=op.f("ck_promise_verdicts_model_verdict"),
        ),
        sa.CheckConstraint(
            "verdict IN ('delivered', 'delivered_late', 'partly_delivered', 'not_delivered', 'walked_back', 'too_early', 'cannot_determine')",
            name=op.f("ck_promise_verdicts_verdict"),
        ),
        sa.ForeignKeyConstraint(
            ["promise_id"],
            ["promises.id"],
            name=op.f("fk_promise_verdicts_promise_id_promises"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("promise_id", name=op.f("pk_promise_verdicts")),
    )


def downgrade() -> None:
    op.drop_table("promise_verdicts")
    op.drop_index("ix_promises_ticker_current", table_name="promises")
    op.drop_index("ix_promises_group_key", table_name="promises")
    op.drop_table("promises")
    op.drop_table("promise_reviews")
