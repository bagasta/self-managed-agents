"""Persist one manager recovery per Group workforce failure.

Revision ID: 034
Revises: 033
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_chat_recoveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("team_chat_rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workforce_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("failure_event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("workforce_task_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manager_message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("team_chat_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("room_id", "failure_event_id", name="uq_team_chat_recovery_failure"),
    )
    op.create_index("ix_team_chat_recoveries_status_created", "team_chat_recoveries", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_team_chat_recoveries_status_created", table_name="team_chat_recoveries")
    op.drop_table("team_chat_recoveries")
