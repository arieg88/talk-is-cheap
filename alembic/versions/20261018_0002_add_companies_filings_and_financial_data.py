"""Add companies, filings and financial data

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("cik", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("fiscal_year_end", sa.String(length=4), nullable=True),
        sa.Column("sic", sa.String(length=8), nullable=True),
        sa.Column("sic_description", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("cik", name=op.f("pk_companies")),
        sa.UniqueConstraint("ticker", name=op.f("uq_companies_ticker")),
    )
    op.create_table(
        "filings",
        sa.Column("accession_no", sa.String(length=20), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("form", sa.String(length=16), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("acceptance_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("primary_document", sa.String(length=255), nullable=True),
        sa.Column("items", sa.String(length=255), nullable=True),
        sa.Column("is_xbrl", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["cik"], ["companies.cik"], name=op.f("fk_filings_cik_companies"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("accession_no", name=op.f("pk_filings")),
    )
    op.create_index(
        "ix_filings_cik_form_filing_date", "filings", ["cik", "form", "filing_date"], unique=False
    )
    op.create_table(
        "financial_values",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("period_type", sa.String(length=8), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=False),
        sa.Column("fiscal_quarter", sa.SmallInteger(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=28, scale=6), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("source_concept", sa.String(length=255), nullable=False),
        sa.Column("accession_no", sa.String(length=20), nullable=False),
        sa.Column("form", sa.String(length=16), nullable=False),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.Column("derived", sa.Boolean(), nullable=False),
        sa.Column("derivation", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "(period_type = 'quarter' AND fiscal_quarter BETWEEN 1 AND 4) OR (period_type = 'year' AND fiscal_quarter IS NULL)",
            name=op.f("ck_financial_values_period_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["cik"],
            ["companies.cik"],
            name=op.f("fk_financial_values_cik_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_values")),
        sa.UniqueConstraint(
            "cik",
            "metric",
            "period_start",
            "period_end",
            "accession_no",
            name="uq_financial_values_version",
        ),
    )
    op.create_index(
        "ix_financial_values_lookup",
        "financial_values",
        ["cik", "metric", "fiscal_year", "fiscal_quarter", "filed"],
        unique=False,
    )
    op.create_table(
        "xbrl_facts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("taxonomy", sa.String(length=32), nullable=False),
        sa.Column("concept", sa.String(length=255), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Numeric(precision=28, scale=6), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("accession_no", sa.String(length=20), nullable=False),
        sa.Column("form", sa.String(length=16), nullable=False),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=True),
        sa.Column("fiscal_period", sa.String(length=4), nullable=True),
        sa.Column("frame", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(
            ["cik"], ["companies.cik"], name=op.f("fk_xbrl_facts_cik_companies"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_xbrl_facts")),
        sa.UniqueConstraint(
            "cik",
            "taxonomy",
            "concept",
            "unit",
            "period_start",
            "period_end",
            "accession_no",
            name="uq_xbrl_facts_identity",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_xbrl_facts_cik_concept", "xbrl_facts", ["cik", "taxonomy", "concept"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_xbrl_facts_cik_concept", table_name="xbrl_facts")
    op.drop_table("xbrl_facts")
    op.drop_index("ix_financial_values_lookup", table_name="financial_values")
    op.drop_table("financial_values")
    op.drop_index("ix_filings_cik_form_filing_date", table_name="filings")
    op.drop_table("filings")
    op.drop_table("companies")
