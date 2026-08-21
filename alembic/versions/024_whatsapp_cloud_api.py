"""Add per-agent WhatsApp Cloud API credentials.

Revision ID: 024
Revises: 023
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("wa_phone_number_id", sa.String(64), nullable=True))
    op.add_column("agents", sa.Column("wa_waba_id", sa.String(64), nullable=True))
    op.add_column("agents", sa.Column("wa_access_token_encrypted", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("wa_display_phone", sa.String(32), nullable=True))
    op.add_column("agents", sa.Column("wa_business_name", sa.String(255), nullable=True))
    op.add_column("agents", sa.Column("wa_connection_type", sa.String(16), nullable=True))
    op.create_index("ix_agents_wa_phone_number_id", "agents", ["wa_phone_number_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_agents_wa_phone_number_id", table_name="agents")
    for name in ("wa_connection_type", "wa_business_name", "wa_display_phone", "wa_access_token_encrypted", "wa_waba_id", "wa_phone_number_id"):
        op.drop_column("agents", name)
