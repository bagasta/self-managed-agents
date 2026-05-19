"""add run usage breakdown

Revision ID: 014
Revises: 013
Create Date: 2026-05-19
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(
        "runs",
        sa.Column("openrouter_cost_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
    )
    op.add_column("runs", sa.Column("usage_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    for column in (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "openrouter_cost_usd",
    ):
        op.alter_column("runs", column, server_default=None)


def downgrade() -> None:
    op.drop_column("runs", "usage_details")
    op.drop_column("runs", "openrouter_cost_usd")
    op.drop_column("runs", "cached_tokens")
    op.drop_column("runs", "reasoning_tokens")
    op.drop_column("runs", "completion_tokens")
    op.drop_column("runs", "prompt_tokens")
