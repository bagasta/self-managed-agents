"""documents_and_temperature

Adds:
- agents.temperature column (float, default 0.7)
- documents table (per-agent knowledge base for RAG retrieval)

Revision ID: 003
Revises: 42559a856c00
Create Date: 2026-04-16

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "42559a856c00"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add temperature to agents
    op.add_column(
        "agents",
        sa.Column(
            "temperature",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.7"),
        ),
    )

    # Create documents table
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=500), nullable=True),
        sa.Column(
            "doc_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_agent_id", "documents", ["agent_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_documents_agent_id", table_name="documents")
    op.drop_table("documents")
    op.drop_column("agents", "temperature")
