"""Add feeds, articles, article feeds and company links, and backfill windows for news

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feeds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=True),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("last_modified", sa.String(length=64), nullable=True),
        sa.Column("items_hash", sa.String(length=64), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_new_item_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feeds")),
        sa.UniqueConstraint("key", name=op.f("uq_feeds_key")),
    )
    op.create_table(
        "articles",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("publisher", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_precision", sa.String(length=16), nullable=False),
        sa.Column("cluster_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "time_precision IN ('published', 'seen', 'fetched')",
            name=op.f("ck_articles_time_precision"),
        ),
        sa.ForeignKeyConstraint(
            ["cluster_id"],
            ["articles.id"],
            name=op.f("fk_articles_cluster_id_articles"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_articles")),
        sa.UniqueConstraint("url_hash", name=op.f("uq_articles_url_hash")),
    )
    op.create_index("ix_articles_cluster_id", "articles", ["cluster_id"], unique=False)
    op.create_index("ix_articles_published_at", "articles", ["published_at"], unique=False)
    op.create_table(
        "article_feeds",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("feed_id", sa.Integer(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_feeds_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["feed_id"],
            ["feeds.id"],
            name=op.f("fk_article_feeds_feed_id_feeds"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("article_id", "feed_id", name=op.f("pk_article_feeds")),
    )
    op.create_index(op.f("ix_article_feeds_feed_id"), "article_feeds", ["feed_id"], unique=False)
    op.create_table(
        "article_companies",
        sa.Column("article_id", sa.BigInteger(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.CheckConstraint(
            "confidence > 0 AND confidence <= 1", name=op.f("ck_article_companies_confidence")
        ),
        sa.CheckConstraint(
            "method IN ('issuer', 'ticker', 'headline', 'summary', 'feed')",
            name=op.f("ck_article_companies_method"),
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_companies_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("article_id", "ticker", name=op.f("pk_article_companies")),
    )
    op.create_index(
        "ix_article_companies_ticker", "article_companies", ["ticker", "confidence"], unique=False
    )
    op.create_table(
        "backfill_windows",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("feed_id", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("items", sa.Integer(), nullable=False),
        sa.Column("new_articles", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["feed_id"],
            ["feeds.id"],
            name=op.f("fk_backfill_windows_feed_id_feeds"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backfill_windows")),
        sa.UniqueConstraint("feed_id", "window_start", name="uq_backfill_windows_window"),
    )


def downgrade() -> None:
    op.drop_table("backfill_windows")
    op.drop_index("ix_article_companies_ticker", table_name="article_companies")
    op.drop_table("article_companies")
    op.drop_index(op.f("ix_article_feeds_feed_id"), table_name="article_feeds")
    op.drop_table("article_feeds")
    op.drop_index("ix_articles_published_at", table_name="articles")
    op.drop_index("ix_articles_cluster_id", table_name="articles")
    op.drop_table("articles")
    op.drop_table("feeds")
