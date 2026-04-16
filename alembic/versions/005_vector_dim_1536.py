"""vector_dim_1536

Switch embedding column from vector(384) (fastembed) to vector(1536)
(OpenRouter openai/text-embedding-3-small).

Existing embeddings are dropped — documents will be re-embedded
automatically the next time their content is updated, or when the
next upload is processed.

Revision ID: 005
Revises: 004
Create Date: 2026-04-16

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_DIM = 384
NEW_DIM = 1536


def upgrade() -> None:
    # Drop old index (tied to the 384-dim column)
    op.execute("DROP INDEX IF EXISTS ix_documents_embedding_hnsw")

    # Replace the column (ALTER TYPE not supported for vector — drop + add)
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS embedding")
    op.execute(f"ALTER TABLE documents ADD COLUMN embedding vector({NEW_DIM})")

    # Recreate HNSW index for the new dimension
    op.execute(
        "CREATE INDEX ix_documents_embedding_hnsw "
        "ON documents USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_embedding_hnsw")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS embedding")
    op.execute(f"ALTER TABLE documents ADD COLUMN embedding vector({OLD_DIM})")
    op.execute(
        "CREATE INDEX ix_documents_embedding_hnsw "
        "ON documents USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
