"""add_max_tokens_to_agents

Revision ID: 24aaaa8cc724
Revises: bf7f59d087e6
Create Date: 2026-04-30 11:23:47.137535

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '24aaaa8cc724'
down_revision: Union[str, None] = 'bf7f59d087e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column('agents', 'max_tokens'):
        op.add_column('agents', sa.Column('max_tokens', sa.Integer(), nullable=True))


def downgrade() -> None:
    if _has_column('agents', 'max_tokens'):
        op.drop_column('agents', 'max_tokens')
