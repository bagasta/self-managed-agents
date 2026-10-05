"""Persist owner-scoped direct and group chats.

Revision ID: 032
Revises: 031
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_chat_rooms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("manager_agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_team_chat_rooms_workspace_updated", "team_chat_rooms", ["workspace_id", "updated_at"])
    op.create_table(
        "team_chat_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("team_chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sessions.id", ondelete="SET NULL")),
        sa.UniqueConstraint("room_id", "agent_id", name="uq_team_chat_member"),
    )
    op.create_table(
        "team_chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("team_chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_type", sa.String(16), nullable=False),
        sa.Column("sender_agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="sent"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_team_chat_messages_room_created", "team_chat_messages", ["room_id", "created_at"])


def downgrade() -> None:
    op.drop_table("team_chat_messages")
    op.drop_table("team_chat_members")
    op.drop_table("team_chat_rooms")
