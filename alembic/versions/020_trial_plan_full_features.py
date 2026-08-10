"""trial plan: full features, 5M tokens, 14-day expiry

Revision ID: 020
Revises: 019
Create Date: 2026-06-10

Perubahan:
  - subscription_plans.trial → max_agents NULL, token_quota 5M, period_days 14,
    allowed_models [], subagents_allowed true
  - user_subscriptions yang masih trial dan belum expire → set expires_at + grace_until
    berdasarkan started_at, dan naikkan token_quota ke 5M
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019_manual_artifact"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TRIAL_ID = "00000000-0000-0000-0000-000000000001"
NEW_TOKEN_QUOTA = 5_000_000
NEW_PERIOD_DAYS = 14
GRACE_DAYS = 3


def upgrade() -> None:
    # 1. Update plan definition
    op.execute(
        f"""
        UPDATE subscription_plans
        SET
            max_agents        = 1,
            token_quota       = {NEW_TOKEN_QUOTA},
            period_days       = {NEW_PERIOD_DAYS},
            allowed_models    = '["openai/gpt-4.1-mini"]'::jsonb,
            subagents_allowed = true
        WHERE id = '{TRIAL_ID}' OR code = 'trial'
        """
    )

    # 2. Bring existing active trial subscriptions up to the new limits.
    #    - expires_at NULL → set to started_at + 14 days (from when they started)
    #    - grace_until NULL → set to expires_at + 3 days
    #    - token_quota raised to 5M if below
    op.execute(
        f"""
        UPDATE user_subscriptions
        SET
            expires_at  = COALESCE(expires_at,  started_at + INTERVAL '{NEW_PERIOD_DAYS} days'),
            grace_until = COALESCE(grace_until, started_at + INTERVAL '{NEW_PERIOD_DAYS + GRACE_DAYS} days'),
            token_quota = GREATEST(token_quota, {NEW_TOKEN_QUOTA})
        WHERE plan_id = '{TRIAL_ID}'
          AND status IN ('trial', 'active')
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE subscription_plans
        SET
            max_agents        = 1,
            token_quota       = 2000000,
            period_days       = NULL,
            allowed_models    = '["openai/gpt-4.1-mini"]'::jsonb,
            subagents_allowed = false
        WHERE id = '{TRIAL_ID}' OR code = 'trial'
        """
    )
    op.execute(
        f"""
        UPDATE user_subscriptions
        SET
            expires_at  = NULL,
            grace_until = NULL,
            token_quota = LEAST(token_quota, 2000000)
        WHERE plan_id = '{TRIAL_ID}'
          AND status IN ('trial', 'active')
        """
    )
