"""Add guidance extractions, guidance with sources and verdicts, and stock splits

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guidance",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("accession_no", sa.String(length=20), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=False),
        sa.Column("fiscal_quarter", sa.SmallInteger(), nullable=False),
        sa.Column("issued_at", sa.Date(), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("metric_raw", sa.Text(), nullable=False),
        sa.Column("segment", sa.Text(), nullable=True),
        sa.Column("basis", sa.String(length=16), nullable=False),
        sa.Column("measure", sa.String(length=24), nullable=False),
        sa.Column("constant_currency", sa.Boolean(), nullable=False),
        sa.Column("period_text", sa.Text(), nullable=False),
        sa.Column("target_fiscal_year", sa.SmallInteger(), nullable=True),
        sa.Column("target_fiscal_quarter", sa.SmallInteger(), nullable=True),
        sa.Column("low", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("high", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("point", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("plus_minus", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("plus_minus_kind", sa.String(length=16), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("qualitative", sa.Text(), nullable=True),
        sa.Column("conditions", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("verdict_reason", sa.Text(), nullable=True),
        sa.Column("basis_assumed", sa.Boolean(), nullable=False),
        sa.Column("actual", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("difference", sa.Numeric(precision=28, scale=6), nullable=True),
        sa.Column("percent_difference", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("actual_accession_no", sa.String(length=20), nullable=True),
        sa.Column("actual_filed", sa.Date(), nullable=True),
        sa.Column(
            "matched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "basis IN ('gaap', 'non_gaap', 'unspecified')", name=op.f("ck_guidance_basis")
        ),
        sa.CheckConstraint(
            "measure IN ('level', 'yoy_growth', 'sequential_growth', 'share_of_revenue')",
            name=op.f("ck_guidance_measure"),
        ),
        sa.CheckConstraint(
            "metric IN ('revenue', 'gross_margin', 'operating_margin', 'operating_expenses', 'operating_income', 'eps', 'capex', 'tax_rate', 'other')",
            name=op.f("ck_guidance_metric"),
        ),
        sa.CheckConstraint(
            "plus_minus_kind IS NULL OR plus_minus_kind IN ('absolute', 'percent')",
            name=op.f("ck_guidance_plus_minus_kind"),
        ),
        sa.CheckConstraint(
            "unit IN ('usd', 'pct', 'usd_per_share', 'other')", name=op.f("ck_guidance_unit")
        ),
        sa.CheckConstraint(
            "verdict IN ('beat', 'met', 'missed', 'pending', 'not_comparable')",
            name=op.f("ck_guidance_verdict"),
        ),
        sa.CheckConstraint(
            "target_fiscal_quarter IS NULL OR target_fiscal_quarter BETWEEN 1 AND 4",
            name=op.f("ck_guidance_target_fiscal_quarter"),
        ),
        sa.ForeignKeyConstraint(
            ["cik"], ["companies.cik"], name=op.f("fk_guidance_cik_companies"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guidance")),
    )
    op.create_index("ix_guidance_accession_no", "guidance", ["accession_no"], unique=False)
    op.create_index(
        "ix_guidance_company_issued",
        "guidance",
        ["cik", "fiscal_year", "fiscal_quarter"],
        unique=False,
    )
    op.create_table(
        "guidance_extractions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("accession_no", sa.String(length=20), nullable=False),
        sa.Column("document_name", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            ["cik"],
            ["companies.cik"],
            name=op.f("fk_guidance_extractions_cik_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guidance_extractions")),
        sa.UniqueConstraint(
            "accession_no", "document_name", name="uq_guidance_extractions_document"
        ),
    )
    op.create_index("ix_guidance_extractions_cik", "guidance_extractions", ["cik"], unique=False)
    op.create_table(
        "stock_splits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("ratio", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint("ratio > 0", name=op.f("ck_stock_splits_ratio")),
        sa.ForeignKeyConstraint(
            ["cik"],
            ["companies.cik"],
            name=op.f("fk_stock_splits_cik_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_splits")),
        sa.UniqueConstraint("cik", "effective_date", name="uq_stock_splits_split"),
    )
    op.create_table(
        "guidance_sources",
        sa.Column("guidance_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("accession_no", sa.String(length=20), nullable=False),
        sa.Column("document_name", sa.String(length=255), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("section_title", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["guidance_id"],
            ["guidance.id"],
            name=op.f("fk_guidance_sources_guidance_id_guidance"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("guidance_id", "position", name=op.f("pk_guidance_sources")),
    )


def downgrade() -> None:
    op.drop_table("guidance_sources")
    op.drop_table("stock_splits")
    op.drop_index("ix_guidance_extractions_cik", table_name="guidance_extractions")
    op.drop_table("guidance_extractions")
    op.drop_index("ix_guidance_company_issued", table_name="guidance")
    op.drop_index("ix_guidance_accession_no", table_name="guidance")
    op.drop_table("guidance")
