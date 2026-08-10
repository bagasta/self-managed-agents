"""WhatsApp Cloud API fields for Meta Embedded Signup.

Revision ID: 024
Revises: 023
Create Date: 2026-08-04

Adds to agents:
  - wa_phone_number_id   VARCHAR(64) NULLABLE — Meta phone number ID
  - wa_waba_id           VARCHAR(64) NULLABLE — WhatsApp Business Account ID
  - wa_access_token_encrypted  TEXT NULLABLE — encrypted System User token
  - wa_display_phone     VARCHAR(32) NULLABLE — display phone number e.g. +628xxx
  - wa_business_name     VARCHAR(255) NULLABLE — business display name
  - wa_connection_type   VARCHAR(16) NULLABLE — "cloud_api" | "legacy"
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "024"
down_revision: Union[str, Sequence[str], None] = "023"
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
    columns = [
        ("wa_phone_number_id", sa.String(64)),
        ("wa_waba_id", sa.String(64)),
        ("wa_access_token_encrypted", sa.Text()),
        ("wa_display_phone", sa.String(32)),
        ("wa_business_name", sa.String(255)),
        ("wa_connection_type", sa.String(16)),
    ]
    for col_name, col_type in columns:
        if not _has_column("agents", col_name):
            op.add_column("agents", sa.Column(col_name, col_type, nullable=True))
    if not _has_index("agents", "ix_agents_wa_phone_number_id"):
        op.create_index(
            "ix_agents_wa_phone_number_id", "agents", ["wa_phone_number_id"], unique=True
        )


def downgrade() -> None:
    if _has_index("agents", "ix_agents_wa_phone_number_id"):
        op.drop_index("ix_agents_wa_phone_number_id", table_name="agents")
    for col_name in (
        "wa_connection_type",
        "wa_business_name",
        "wa_display_phone",
        "wa_access_token_encrypted",
        "wa_waba_id",
        "wa_phone_number_id",
    ):
        if _has_column("agents", col_name):
            op.drop_column("agents", col_name)
