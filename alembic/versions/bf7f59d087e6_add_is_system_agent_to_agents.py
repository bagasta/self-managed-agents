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


def upgrade() -> None:
    # Tambah flag is_system_agent ke tabel agents
    # server_default='false' agar semua agent lama otomatis False (aman)
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
    op.drop_column('agents', 'is_system_agent')
