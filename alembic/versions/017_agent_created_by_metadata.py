"""add agent created-by metadata

Revision ID: 017
Revises: 015, 016
Create Date: 2026-05-29
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "017"
down_revision: Union[str, tuple[str, ...], None] = ("015", "016")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("agents", "created_by_type"):
        op.add_column("agents", sa.Column("created_by_type", sa.String(32), nullable=True))
    if not _has_column("agents", "created_by_agent_id"):
        op.add_column("agents", sa.Column("created_by_agent_id", sa.String(64), nullable=True))
    if not _has_column("agents", "created_by_agent_name"):
        op.add_column("agents", sa.Column("created_by_agent_name", sa.String(255), nullable=True))


def downgrade() -> None:
    if _has_column("agents", "created_by_agent_name"):
        op.drop_column("agents", "created_by_agent_name")
    if _has_column("agents", "created_by_agent_id"):
        op.drop_column("agents", "created_by_agent_id")
    if _has_column("agents", "created_by_type"):
        op.drop_column("agents", "created_by_type")
