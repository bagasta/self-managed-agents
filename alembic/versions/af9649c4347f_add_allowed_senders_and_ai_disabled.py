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


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    # Fitur 1: Allowlist pengirim pada Agent
    if not _has_column('agents', 'allowed_senders'):
        op.add_column('agents', sa.Column(
            'allowed_senders',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ))

    # Fitur 2: Tombol on/off AI per sesi pengguna
    if not _has_column('sessions', 'ai_disabled'):
        op.add_column('sessions', sa.Column(
            'ai_disabled',
            sa.Boolean(),
            nullable=False,
            server_default='false',
        ))


def downgrade() -> None:
    if _has_column('sessions', 'ai_disabled'):
        op.drop_column('sessions', 'ai_disabled')
    if _has_column('agents', 'allowed_senders'):
        op.drop_column('agents', 'allowed_senders')
