"""agent quota and subscription fields

Revision ID: 007
Revises: 006
Create Date: 2026-04-20 00:00:00.000000

Adds to agents:
  - api_key       VARCHAR(64) UNIQUE — per-agent auth key
  - token_quota   BIGINT     — max tokens allowed in the active period (default 4_000_000)
  - tokens_used   BIGINT     — tokens consumed since last renewal (default 0)
  - active_until  TIMESTAMPTZ — subscription expiry (default now + 30 days)
  - quota_period_days INT    — period length in days used for renewal (default 30)
"""
from __future__ import annotations

import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "007"
down_revision: Union[str, Sequence[str], None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "api_key",
            sa.String(64),
            nullable=False,
            server_default=sa.text("replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', '')"),
        ),
    )
    op.create_index("ix_agents_api_key", "agents", ["api_key"], unique=True)

    op.add_column(
        "agents",
        sa.Column("token_quota", sa.BigInteger, nullable=False, server_default="4000000"),
    )
    op.add_column(
        "agents",
        sa.Column("tokens_used", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.add_column(
        "agents",
        sa.Column(
            "active_until",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW() + INTERVAL '30 days'"),
        ),
    )
    op.add_column(
        "agents",
        sa.Column("quota_period_days", sa.Integer, nullable=False, server_default="30"),
    )


def downgrade() -> None:
    op.drop_index("ix_agents_api_key", table_name="agents")
    op.drop_column("agents", "api_key")
    op.drop_column("agents", "token_quota")
    op.drop_column("agents", "tokens_used")
    op.drop_column("agents", "active_until")
    op.drop_column("agents", "quota_period_days")
