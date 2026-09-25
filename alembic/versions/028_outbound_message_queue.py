"""Add durable outbound WhatsApp queue.

Revision ID: 028
Revises: 027
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbound_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("source_device_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="reply_to_customer"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbound_messages_due", "outbound_messages", ["status", "available_at"])
    op.create_index("ix_outbound_messages_target", "outbound_messages", ["agent_id", "target", "status"])


def downgrade() -> None:
    op.drop_index("ix_outbound_messages_target", table_name="outbound_messages")
    op.drop_index("ix_outbound_messages_due", table_name="outbound_messages")
    op.drop_table("outbound_messages")
