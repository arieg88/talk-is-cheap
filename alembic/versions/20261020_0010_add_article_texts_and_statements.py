"""Add fetched article texts, and statements in the news with their outcomes and reviews

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article_texts",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('ok', 'unavailable')", name=op.f("ck_article_texts_status")),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_texts_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("article_id", name=op.f("pk_article_texts")),
    )
    op.create_table(
        "statement_extractions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("cluster_id", sa.BigInteger(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("processing_version", sa.String(length=16), nullable=False),
        sa.Column("claims", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dropped", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_statement_extractions_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_extractions")),
        sa.UniqueConstraint("article_id", "ticker", name="uq_statement_extractions_article"),
    )
    op.create_index(
        "ix_statement_extractions_ticker", "statement_extractions", ["ticker"], unique=False
    )
    op.create_table(
        "statement_reviews",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
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
            "decision IN ('ok', 'wrong')", name=op.f("ck_statement_reviews_decision")
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_statement_reviews_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_reviews")),
        sa.UniqueConstraint("article_id", "ticker", "item_key", name="uq_statement_reviews_item"),
    )
    op.create_table(
        "statements",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("extraction_id", sa.BigInteger(), nullable=False),
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("cluster_id", sa.BigInteger(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("speaker_name", sa.Text(), nullable=True),
        sa.Column("speaker_role", sa.Text(), nullable=True),
        sa.Column("attribution", sa.String(length=24), nullable=False),
        sa.Column("executive_name", sa.Text(), nullable=True),
        sa.Column("executive_role", sa.Text(), nullable=True),
        sa.Column("management", sa.Boolean(), nullable=False),
        sa.Column("is_direct_quote", sa.Boolean(), nullable=False),
        sa.Column("venue", sa.String(length=16), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("date_from_article", sa.Boolean(), nullable=False),
        sa.Column("claim_type", sa.String(length=24), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=True),
        sa.Column("metric_raw", sa.Text(), nullable=True),
        sa.Column("segment", sa.Text(), nullable=True),
        sa.Column("basis", sa.String(length=16), nullable=False),
        sa.Column("measure", sa.String(length=24), nullable=False),
        sa.Column("constant_currency", sa.Boolean(), nullable=False),
        sa.Column("period_text", sa.Text(), nullable=True),
        sa.Column("target_fiscal_year", sa.SmallInteger(), nullable=True),
        sa.Column("target_fiscal_quarter", sa.SmallInteger(), nullable=True),
        sa.Column("low", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("high", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("point", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=True),
        sa.Column("item_key", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "attribution IN ('named_executive', 'unverified_executive', 'company_spokesperson', 'company_statement', 'analyst', 'anonymous_source', 'reporter')",
            name=op.f("ck_statements_attribution"),
        ),
        sa.CheckConstraint(
            "claim_type IN ('numeric_forecast', 'directional', 'plan', 'qualitative')",
            name=op.f("ck_statements_claim_type"),
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('increase', 'decrease', 'flat')",
            name=op.f("ck_statements_direction"),
        ),
        sa.CheckConstraint(
            "unit IN ('usd', 'pct', 'usd_per_share', 'other')", name=op.f("ck_statements_unit")
        ),
        sa.CheckConstraint(
            "venue IN ('interview', 'conference', 'earnings_call', 'press_release', 'social_media', 'other', 'unknown')",
            name=op.f("ck_statements_venue"),
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_statements_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["statement_extractions.id"],
            name=op.f("fk_statements_extraction_id_statement_extractions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statements")),
    )
    op.create_index("ix_statements_extraction_id", "statements", ["extraction_id"], unique=False)
    op.create_index(
        "ix_statements_ticker_date", "statements", ["ticker", "statement_date"], unique=False
    )
    op.create_table(
        "statement_outcomes",
        sa.Column("statement_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("verdict_reason", sa.Text(), nullable=True),
        sa.Column("actual", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("prior_actual", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("difference", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("percent_difference", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("actual_accession_no", sa.String(length=20), nullable=True),
        sa.Column("actual_filed", sa.Date(), nullable=True),
        sa.Column("plausibility", sa.String(length=16), nullable=True),
        sa.Column("plausibility_rule", sa.String(length=32), nullable=True),
        sa.Column("plausibility_note", sa.Text(), nullable=True),
        sa.Column("review", sa.String(length=8), nullable=True),
        sa.Column("guidance_comparison", sa.String(length=24), nullable=False),
        sa.Column("guidance_id", sa.BigInteger(), nullable=True),
        sa.Column("guidance_note", sa.Text(), nullable=True),
        sa.Column("context_url", sa.Text(), nullable=True),
        sa.Column("context_label", sa.Text(), nullable=True),
        sa.Column("context_text", sa.Text(), nullable=True),
        sa.Column(
            "matched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "guidance_comparison IN ('consistent', 'differs', 'no_formal_guidance')",
            name=op.f("ck_statement_outcomes_guidance_comparison"),
        ),
        sa.CheckConstraint(
            "plausibility IS NULL OR plausibility IN ('passed', 'flagged', 'skipped')",
            name=op.f("ck_statement_outcomes_plausibility"),
        ),
        sa.CheckConstraint(
            "review IS NULL OR review IN ('ok', 'wrong')", name=op.f("ck_statement_outcomes_review")
        ),
        sa.CheckConstraint(
            "verdict IN ('beat', 'met', 'missed', 'pending', 'not_comparable', 'confirmed', 'contradicted', 'not_checkable')",
            name=op.f("ck_statement_outcomes_verdict"),
        ),
        sa.ForeignKeyConstraint(
            ["guidance_id"],
            ["guidance.id"],
            name=op.f("fk_statement_outcomes_guidance_id_guidance"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["statements.id"],
            name=op.f("fk_statement_outcomes_statement_id_statements"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("statement_id", name=op.f("pk_statement_outcomes")),
    )


def downgrade() -> None:
    op.drop_table("statement_outcomes")
    op.drop_index("ix_statements_ticker_date", table_name="statements")
    op.drop_index("ix_statements_extraction_id", table_name="statements")
    op.drop_table("statements")
    op.drop_table("statement_reviews")
    op.drop_index("ix_statement_extractions_ticker", table_name="statement_extractions")
    op.drop_table("statement_extractions")
    op.drop_table("article_texts")
