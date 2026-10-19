"""Add chunks with full-text search, and embeddings with an HNSW index per model

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The default embedding model's index. `talk-is-cheap index` creates the index of any
# other model the first time it is used (retrieval/store.py: ensure_vector_index).
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_INDEX = "ix_embeddings_hnsw_baai_bge_small_en_v1_5"
DEFAULT_DIMENSIONS = 384


def upgrade() -> None:
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=True),
        sa.Column("section_id", sa.BigInteger(), nullable=True),
        sa.Column("article_id", sa.BigInteger(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("tickers", postgresql.ARRAY(sa.String(length=16)), nullable=False),
        sa.Column("fiscal_year", sa.SmallInteger(), nullable=True),
        sa.Column("fiscal_quarter", sa.SmallInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("section_type", sa.String(length=32), nullable=True),
        sa.Column("header", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "search_text",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(source = 'filing') = (document_id IS NOT NULL AND section_id IS NOT NULL AND fiscal_year IS NOT NULL AND fiscal_quarter IS NOT NULL)",
            name=op.f("ck_chunks_filing_fields"),
        ),
        sa.CheckConstraint(
            "(source = 'news') = (article_id IS NOT NULL)", name=op.f("ck_chunks_news_fields")
        ),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end >= char_start", name=op.f("ck_chunks_char_range")
        ),
        sa.CheckConstraint("source IN ('filing', 'news')", name=op.f("ck_chunks_source")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
    )
    op.create_index("ix_chunks_article_id", "chunks", ["article_id"], unique=False)
    op.create_index("ix_chunks_content_hash", "chunks", ["content_hash"], unique=False)
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"], unique=False)
    op.create_index("ix_chunks_period", "chunks", ["fiscal_year", "fiscal_quarter"], unique=False)
    op.create_index("ix_chunks_published_at", "chunks", ["published_at"], unique=False)
    op.create_index(
        "ix_chunks_search_text", "chunks", ["search_text"], unique=False, postgresql_using="gin"
    )
    op.create_index("ix_chunks_text_hash", "chunks", ["text_hash"], unique=False)
    op.create_index(
        "ix_chunks_tickers", "chunks", ["tickers"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "embeddings",
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("model", "content_hash", name=op.f("pk_embeddings")),
    )
    # HNSW needs a fixed dimension: index the rows of one model, cast to its dimension.
    op.execute(
        f"CREATE INDEX {DEFAULT_INDEX} ON embeddings USING hnsw "
        f"((embedding::vector({DEFAULT_DIMENSIONS})) vector_cosine_ops) "
        f"WHERE model = '{DEFAULT_MODEL}'"
    )


def downgrade() -> None:
    op.drop_table("embeddings")  # drops every model's vector index with it
    op.drop_index("ix_chunks_tickers", table_name="chunks", postgresql_using="gin")
    op.drop_index("ix_chunks_text_hash", table_name="chunks")
    op.drop_index("ix_chunks_search_text", table_name="chunks", postgresql_using="gin")
    op.drop_index("ix_chunks_published_at", table_name="chunks")
    op.drop_index("ix_chunks_period", table_name="chunks")
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.drop_index("ix_chunks_content_hash", table_name="chunks")
    op.drop_index("ix_chunks_article_id", table_name="chunks")
    op.drop_table("chunks")
