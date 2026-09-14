"""Add an explicit execution mode for scheduled jobs.

Revision ID: 027
Revises: 026
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_jobs",
        sa.Column("execution_mode", sa.String(32), nullable=False, server_default="reminder"),
    )


def downgrade() -> None:
    op.drop_column("scheduled_jobs", "execution_mode")
