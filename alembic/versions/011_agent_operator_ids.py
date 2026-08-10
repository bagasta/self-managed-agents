"""Add operator_ids to agents table

Revision ID: 011
Revises: 010
Create Date: 2026-04-23

Adds operator_ids (JSONB list of phone/JID strings) to the agents table.
Used to identify operator users without relying solely on escalation_config.operator_phone.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "011"
down_revision: Union[str, Sequence[str], None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("agents", "operator_ids"):
        op.add_column(
            "agents",
            sa.Column(
                "operator_ids",
                JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )


def downgrade() -> None:
    if _has_column("agents", "operator_ids"):
        op.drop_column("agents", "operator_ids")
