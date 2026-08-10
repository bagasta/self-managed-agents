"""pgvector_embeddings

Enables the pgvector extension and adds an embedding vector(384) column to
the documents table for semantic (cosine similarity) search.

Prerequisites:
  The PostgreSQL user must have CREATE EXTENSION privilege, or the extension
  must already be installed by a superuser:
    psql -c "CREATE EXTENSION IF NOT EXISTS vector;" <db>

Revision ID: 004
Revises: 003
Create Date: 2026-04-16

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 384


def upgrade() -> None:
    # Enable pgvector — requires PostgreSQL extension installed on the server.
    # If this fails, run: psql -c "CREATE EXTENSION IF NOT EXISTS vector;" on
    # your DB as a superuser, then re-run the migration.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Add embedding column (nullable — existing docs won't have embeddings yet;
    # they'll be backfilled when content is next updated, or left as NULL and
    # the keyword fallback search will handle them).
    op.execute(
        f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS "
        f"embedding vector({EMBEDDING_DIM})"
    )

    # Optional HNSW index for fast approximate nearest-neighbour search.
    # Comment this out if your pgvector version < 0.5.0 (use ivfflat instead).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_embedding_hnsw "
        "ON documents USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_embedding_hnsw")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS embedding")
    # Note: we intentionally do NOT drop the vector extension here because
    # other tables/columns may depend on it.
