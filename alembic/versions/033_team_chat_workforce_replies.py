"""Link group bot replies to durable workforce steps.

Revision ID: 033
Revises: 032
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "team_chat_messages",
        sa.Column(
            "workforce_step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workforce_task_steps.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_team_chat_message_workforce_step",
        "team_chat_messages",
        ["room_id", "workforce_step_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_team_chat_message_workforce_step", "team_chat_messages", type_="unique")
    op.drop_column("team_chat_messages", "workforce_step_id")
