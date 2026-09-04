"""Add exclusive per-agent WhatsApp inbound routing.

Revision ID: 026
Revises: 025
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("wa_inbound_route", sa.String(16), nullable=False, server_default="ai_staff"),
    )
    op.add_column("agents", sa.Column("wa_n8n_webhook_url_encrypted", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("wa_n8n_webhook_secret_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "wa_n8n_webhook_secret_encrypted")
    op.drop_column("agents", "wa_n8n_webhook_url_encrypted")
    op.drop_column("agents", "wa_inbound_route")
