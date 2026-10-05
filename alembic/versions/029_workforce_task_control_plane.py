"""Add durable workforce task control-plane records.

Revision ID: 029
Revises: 028
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workforce_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("parent_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("customer_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["assigned_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_task_id"], ["workforce_tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_workforce_tasks_workspace_idempotency"),
    )
    op.create_index("ix_workforce_tasks_workspace_id", "workforce_tasks", ["workspace_id"])
    op.create_index("ix_workforce_tasks_workspace_status", "workforce_tasks", ["workspace_id", "status"])
    op.create_index("ix_workforce_tasks_workspace_assignee", "workforce_tasks", ["workspace_id", "assigned_agent_id"])

    op.create_table(
        "workforce_task_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("input_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("delegated_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["assigned_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["delegated_by_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["workforce_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_workforce_task_steps_task_sequence"),
    )
    op.create_index("ix_workforce_task_steps_task_status", "workforce_task_steps", ["task_id", "status"])

    op.create_table(
        "workforce_task_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="workspace"),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["actor_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["step_id"], ["workforce_task_steps.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["workforce_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workforce_task_events_task_created", "workforce_task_events", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_workforce_task_events_task_created", table_name="workforce_task_events")
    op.drop_table("workforce_task_events")
    op.drop_index("ix_workforce_task_steps_task_status", table_name="workforce_task_steps")
    op.drop_table("workforce_task_steps")
    op.drop_index("ix_workforce_tasks_workspace_assignee", table_name="workforce_tasks")
    op.drop_index("ix_workforce_tasks_workspace_status", table_name="workforce_tasks")
    op.drop_index("ix_workforce_tasks_workspace_id", table_name="workforce_tasks")
    op.drop_table("workforce_tasks")
