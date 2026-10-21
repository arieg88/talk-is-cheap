"""Add archive searches and captures, publisher URLs of articles, the date time
precision, and the subject kind and key of promises

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "archive_searches",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("publisher", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("period", sa.String(length=32), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("captures", sa.Integer(), nullable=False),
        sa.Column("kept", sa.Integer(), nullable=False),
        sa.Column("months", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "searched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('commoncrawl', 'wayback')", name=op.f("ck_archive_searches_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_searches")),
        sa.UniqueConstraint("publisher", "source", "period", "prefix", name="uq_archive_searches"),
    )
    op.create_table(
        "archive_captures",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("publisher", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("captured_url", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("warc_filename", sa.Text(), nullable=True),
        sa.Column("warc_offset", sa.BigInteger(), nullable=True),
        sa.Column("warc_length", sa.Integer(), nullable=True),
        sa.Column("url_date", sa.Date(), nullable=True),
        sa.Column("tickers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("article_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "found_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('commoncrawl', 'wayback')", name=op.f("ck_archive_captures_source")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'stored', 'unavailable', 'skipped')",
            name=op.f("ck_archive_captures_status"),
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_archive_captures_article_id_articles"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_captures")),
        sa.UniqueConstraint("url_hash", name=op.f("uq_archive_captures_url_hash")),
    )
    op.create_index(
        "ix_archive_captures_publisher_status", "archive_captures", ["publisher", "status"]
    )
    op.add_column("articles", sa.Column("publisher_url", sa.Text(), nullable=True))
    op.add_column(
        "articles",
        sa.Column("publisher_url_score", sa.Numeric(precision=5, scale=2), nullable=True),
    )
    op.add_column(
        "articles", sa.Column("publisher_url_source", sa.String(length=16), nullable=True)
    )
    op.drop_constraint(op.f("ck_articles_time_precision"), "articles", type_="check")
    op.create_check_constraint(
        op.f("ck_articles_time_precision"),
        "articles",
        "time_precision IN ('published', 'date', 'seen', 'fetched')",
    )
    op.add_column(
        "promises",
        sa.Column("subject_kind", sa.String(length=16), server_default="company", nullable=False),
    )
    op.add_column("promises", sa.Column("subject_key", sa.String(length=32), nullable=True))
    op.execute("UPDATE promises SET subject_key = ticker")
    op.create_check_constraint(
        op.f("ck_promises_subject_kind"),
        "promises",
        "subject_kind IN ('company', 'index', 'sector', 'macro')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_promises_subject_kind"), "promises", type_="check")
    op.drop_column("promises", "subject_key")
    op.drop_column("promises", "subject_kind")
    op.execute("UPDATE articles SET time_precision = 'published' WHERE time_precision = 'date'")
    op.drop_constraint(op.f("ck_articles_time_precision"), "articles", type_="check")
    op.create_check_constraint(
        op.f("ck_articles_time_precision"),
        "articles",
        "time_precision IN ('published', 'seen', 'fetched')",
    )
    op.drop_column("articles", "publisher_url_source")
    op.drop_column("articles", "publisher_url_score")
    op.drop_column("articles", "publisher_url")
    op.drop_index("ix_archive_captures_publisher_status", table_name="archive_captures")
    op.drop_table("archive_captures")
    op.drop_table("archive_searches")
