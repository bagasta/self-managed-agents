"""channels, escalation, scheduled_jobs

Revision ID: 006
Revises: 005, 42559a856c00
Create Date: 2026-04-20 00:00:00.000000

Adds:
  - agents.escalation_config (JSONB)
  - sessions.channel_type, channel_config, escalation_active
  - table: scheduled_jobs
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "006"
down_revision: Union[str, Sequence[str], None] = ("005", "42559a856c00")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    # --- agents: add escalation_config ---
    if not _has_column("agents", "escalation_config"):
        op.add_column(
            "agents",
            sa.Column("escalation_config", JSONB, nullable=False, server_default="{}"),
        )

    # --- sessions: add channel fields ---
    if not _has_column("sessions", "channel_type"):
        op.add_column(
            "sessions",
            sa.Column("channel_type", sa.String(64), nullable=True),
        )
    if not _has_column("sessions", "channel_config"):
        op.add_column(
            "sessions",
            sa.Column("channel_config", JSONB, nullable=False, server_default="{}"),
        )
    if not _has_column("sessions", "escalation_active"):
        op.add_column(
            "sessions",
            sa.Column(
                "escalation_active",
                sa.Boolean,
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    # --- scheduled_jobs: new table ---
    if not _has_table("scheduled_jobs"):
        op.create_table(
            "scheduled_jobs",
            sa.Column(
                "id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "agent_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "session_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("label", sa.String(255), nullable=False),
            sa.Column("cron_expr", sa.String(255), nullable=True),
            sa.Column("run_once_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("payload", sa.Text, nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="active"),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
    if not _has_index("scheduled_jobs", "ix_scheduled_jobs_session_id"):
        op.create_index("ix_scheduled_jobs_session_id", "scheduled_jobs", ["session_id"])
    if not _has_index("scheduled_jobs", "ix_scheduled_jobs_next_run_at"):
        op.create_index("ix_scheduled_jobs_next_run_at", "scheduled_jobs", ["next_run_at"])
    if not _has_index("scheduled_jobs", "ix_scheduled_jobs_status"):
        op.create_index("ix_scheduled_jobs_status", "scheduled_jobs", ["status"])


def downgrade() -> None:
    if _has_table("scheduled_jobs"):
        op.drop_table("scheduled_jobs")
    if _has_column("sessions", "escalation_active"):
        op.drop_column("sessions", "escalation_active")
    if _has_column("sessions", "channel_config"):
        op.drop_column("sessions", "channel_config")
    if _has_column("sessions", "channel_type"):
        op.drop_column("sessions", "channel_type")
    if _has_column("agents", "escalation_config"):
        op.drop_column("agents", "escalation_config")
