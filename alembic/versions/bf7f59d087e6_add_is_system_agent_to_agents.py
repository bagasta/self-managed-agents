"""add_is_system_agent_to_agents

Revision ID: bf7f59d087e6
Revises: af9649c4347f
Create Date: 2026-04-28 15:08:10.856677

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bf7f59d087e6'
down_revision: Union[str, None] = 'af9649c4347f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    # Tambah flag is_system_agent ke tabel agents
    # server_default='false' agar semua agent lama otomatis False (aman)
    if not _has_column('agents', 'is_system_agent') and not _has_column('agents', 'capabilities'):
        op.add_column(
            'agents',
            sa.Column(
                'is_system_agent',
                sa.Boolean(),
                nullable=False,
                server_default='false',
            ),
        )


def downgrade() -> None:
    if _has_column('agents', 'is_system_agent'):
        op.drop_column('agents', 'is_system_agent')
