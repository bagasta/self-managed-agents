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


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _has_unique_constraint(table_name: str, constraint_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(c["name"] == constraint_name for c in inspector.get_unique_constraints(table_name))


def upgrade() -> None:
    if not _has_column("agent_memories", "scope"):
        op.add_column(
            "agent_memories",
            sa.Column("scope", sa.String(255), nullable=True),
        )
    # Drop old unique constraint (agent_id, key)
    if _has_unique_constraint("agent_memories", "uq_agent_memory_key"):
        op.drop_constraint("uq_agent_memory_key", "agent_memories", type_="unique")
    # New unique constraint (agent_id, scope, key) — NULL scope is treated as global
    if not _has_unique_constraint("agent_memories", "uq_agent_memory_scope_key"):
        op.create_unique_constraint(
            "uq_agent_memory_scope_key",
            "agent_memories",
            ["agent_id", "scope", "key"],
        )


def downgrade() -> None:
    if _has_unique_constraint("agent_memories", "uq_agent_memory_scope_key"):
        op.drop_constraint("uq_agent_memory_scope_key", "agent_memories", type_="unique")
    if _has_column("agent_memories", "scope"):
        op.drop_column("agent_memories", "scope")
    if not _has_unique_constraint("agent_memories", "uq_agent_memory_key"):
        op.create_unique_constraint(
            "uq_agent_memory_key", "agent_memories", ["agent_id", "key"]
        )
