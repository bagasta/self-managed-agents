"""set tier 3 token quota to 100m

Revision ID: 016
Revises: 014
Create Date: 2026-05-26
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "016"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TIER_3_ID = "00000000-0000-0000-0000-000000000004"
TIER_3_TOKEN_QUOTA = 100_000_000


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE subscription_plans
        SET token_quota = {TIER_3_TOKEN_QUOTA}
        WHERE id = '{TIER_3_ID}' OR code = 'tier_3'
        """
    )
    op.execute(
        f"""
        UPDATE user_subscriptions
        SET token_quota = {TIER_3_TOKEN_QUOTA}
        WHERE plan_id = '{TIER_3_ID}' AND token_quota = 0
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE user_subscriptions
        SET token_quota = 0
        WHERE plan_id = '{TIER_3_ID}' AND token_quota = {TIER_3_TOKEN_QUOTA}
        """
    )
    op.execute(
        f"""
        UPDATE subscription_plans
        SET token_quota = 0
        WHERE id = '{TIER_3_ID}' OR code = 'tier_3'
        """
    )
