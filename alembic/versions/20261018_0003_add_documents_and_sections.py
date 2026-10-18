"""Add documents and sections for parsed filing text

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("accession_no", sa.String(length=20), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("document_name", sa.String(length=255), nullable=False),
        sa.Column("exhibit", sa.String(length=32), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=False),
        sa.Column("fiscal_quarter", sa.SmallInteger(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("filed_at", sa.Date(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=16), nullable=False),
        sa.Column(
            "parsed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "doc_type IN ('press_release', 'earnings_exhibit', '10q', '10k', 'deliveries_report', 'other_exhibit')",
            name=op.f("ck_documents_doc_type"),
        ),
        sa.CheckConstraint(
            "fiscal_quarter BETWEEN 1 AND 4", name=op.f("ck_documents_fiscal_quarter")
        ),
        sa.ForeignKeyConstraint(
            ["accession_no"],
            ["filings.accession_no"],
            name=op.f("fk_documents_accession_no_filings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["cik"], ["companies.cik"], name=op.f("fk_documents_cik_companies"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("accession_no", "document_name", name="uq_documents_document"),
    )
    op.create_index(
        "ix_documents_company_period",
        "documents",
        ["cik", "fiscal_year", "fiscal_quarter", "doc_type"],
        unique=False,
    )
    op.create_table(
        "sections",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("section_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end >= char_start", name=op.f("ck_sections_char_range")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_sections_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sections")),
        sa.UniqueConstraint("document_id", "position", name="uq_sections_position"),
    )
    op.create_index("ix_sections_section_type", "sections", ["section_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sections_section_type", table_name="sections")
    op.drop_table("sections")
    op.drop_index("ix_documents_company_period", table_name="documents")
    op.drop_table("documents")
