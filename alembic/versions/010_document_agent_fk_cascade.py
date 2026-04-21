"""Add FK cascade from documents.agent_id to agents.id

Revision ID: 010
Revises: 009
Create Date: 2026-04-21

Without this FK, deleting an agent left its documents as orphaned rows.
After this migration, PostgreSQL will automatically DELETE all documents
belonging to an agent when that agent is deleted.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: Union[str, Sequence[str], None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop any orphaned documents whose agent no longer exists (safety cleanup)
    op.execute(
        "DELETE FROM documents WHERE agent_id NOT IN (SELECT id FROM agents)"
    )
    # Add the FK constraint with CASCADE
    op.create_foreign_key(
        "fk_documents_agent_id",
        "documents",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_documents_agent_id", "documents", type_="foreignkey")
