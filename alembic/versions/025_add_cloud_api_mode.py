"""Track whether a Cloud API number uses WhatsApp Business App Coexistence.

Revision ID: 025
Revises: 024
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("wa_cloud_api_mode", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "wa_cloud_api_mode")
