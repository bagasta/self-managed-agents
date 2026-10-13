"""Persist files attached to owner Team Chat messages.

Revision ID: 035
Revises: 034
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_chat_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("team_chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("team_chat_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_team_chat_attachments_room_created", "team_chat_attachments", ["room_id", "created_at"])
    op.create_index("ix_team_chat_attachments_message", "team_chat_attachments", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_team_chat_attachments_message", table_name="team_chat_attachments")
    op.drop_index("ix_team_chat_attachments_room_created", table_name="team_chat_attachments")
    op.drop_table("team_chat_attachments")
