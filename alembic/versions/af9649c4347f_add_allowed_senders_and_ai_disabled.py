"""add_allowed_senders_and_ai_disabled

Revision ID: af9649c4347f
Revises: 011
Create Date: 2026-04-28 09:32:13.831136

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'af9649c4347f'
down_revision: Union[str, None] = '011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fitur 1: Allowlist pengirim pada Agent
    op.add_column('agents', sa.Column(
        'allowed_senders',
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=True,
    ))

    # Fitur 2: Tombol on/off AI per sesi pengguna
    op.add_column('sessions', sa.Column(
        'ai_disabled',
        sa.Boolean(),
        nullable=False,
        server_default='false',
    ))


def downgrade() -> None:
    op.drop_column('sessions', 'ai_disabled')
    op.drop_column('agents', 'allowed_senders')
