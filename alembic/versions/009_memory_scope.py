"""Add scope to agent_memories for per-phone isolation.

Revision ID: 009
Revises: 008_agent_whatsapp
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_memories",
        sa.Column("scope", sa.String(255), nullable=True),
    )
    # Drop old unique constraint (agent_id, key)
    op.drop_constraint("uq_agent_memory_key", "agent_memories", type_="unique")
    # New unique constraint (agent_id, scope, key) — NULL scope is treated as global
    op.create_unique_constraint(
        "uq_agent_memory_scope_key",
        "agent_memories",
        ["agent_id", "scope", "key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_agent_memory_scope_key", "agent_memories", type_="unique")
    op.drop_column("agent_memories", "scope")
    op.create_unique_constraint(
        "uq_agent_memory_key", "agent_memories", ["agent_id", "key"]
    )
