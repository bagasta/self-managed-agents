"""Add optional owner binding for user API keys.

Revision ID: 030
Revises: 029
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_api_keys", sa.Column("owner_external_id", sa.String(length=64), nullable=True))
    op.create_index("ix_user_api_keys_owner_external_id", "user_api_keys", ["owner_external_id"])


def downgrade() -> None:
    op.drop_index("ix_user_api_keys_owner_external_id", table_name="user_api_keys")
    op.drop_column("user_api_keys", "owner_external_id")
