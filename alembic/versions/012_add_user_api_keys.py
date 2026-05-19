"""add_user_api_keys

Revision ID: 012
Revises: af9649c4347f
Create Date: 2026-05-11

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "6f5f935962da"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _has_table("user_api_keys"):
        op.create_table(
            "user_api_keys",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("key_hash", sa.String(64), nullable=False),
            sa.Column("label", sa.String(255), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
    if not _has_index("user_api_keys", "ix_user_api_keys_key_hash"):
        op.create_index("ix_user_api_keys_key_hash", "user_api_keys", ["key_hash"], unique=True)


def downgrade() -> None:
    if _has_index("user_api_keys", "ix_user_api_keys_key_hash"):
        op.drop_index("ix_user_api_keys_key_hash", table_name="user_api_keys")
    if _has_table("user_api_keys"):
        op.drop_table("user_api_keys")
