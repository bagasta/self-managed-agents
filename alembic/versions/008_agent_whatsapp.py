"""agent whatsapp channel fields

Revision ID: 008
Revises: 007
Create Date: 2026-04-20 00:00:00.000000

Adds to agents:
  - wa_device_id  VARCHAR(64) NULLABLE — maps to Go wa-service device
  - channel_type  VARCHAR(32) NULLABLE — e.g. "whatsapp", "telegram"
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, Sequence[str], None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    if not _has_column("agents", "wa_device_id"):
        op.add_column("agents", sa.Column("wa_device_id", sa.String(64), nullable=True))
    if not _has_column("agents", "channel_type"):
        op.add_column("agents", sa.Column("channel_type", sa.String(32), nullable=True))
    if not _has_index("agents", "ix_agents_wa_device_id"):
        op.create_index("ix_agents_wa_device_id", "agents", ["wa_device_id"], unique=True)


def downgrade() -> None:
    if _has_index("agents", "ix_agents_wa_device_id"):
        op.drop_index("ix_agents_wa_device_id", table_name="agents")
    if _has_column("agents", "wa_device_id"):
        op.drop_column("agents", "wa_device_id")
    if _has_column("agents", "channel_type"):
        op.drop_column("agents", "channel_type")
