"""Replace the legacy paid tiers with Growth and Business.

Revision ID: 029
Revises: 028
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None

TRIAL_ID = "00000000-0000-0000-0000-000000000001"
GROWTH_ID = "00000000-0000-0000-0000-000000000002"
LEGACY_PRO_ID = "00000000-0000-0000-0000-000000000003"
BUSINESS_ID = "00000000-0000-0000-0000-000000000004"


def upgrade() -> None:
    op.add_column(
        "subscription_plans",
        sa.Column("monthly_price_idr", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "subscription_plans",
        sa.Column("monthly_message_limit", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "subscription_plans",
        sa.Column("marketing_features", postgresql.JSONB(), nullable=False, server_default="[]"),
    )

    op.execute(
        f"""
        UPDATE subscription_plans
        SET label = 'Trial', max_agents = 1, monthly_price_idr = 0,
            monthly_message_limit = 500,
            marketing_features = '["1 asisten AI", "500 pesan / bulan", "Dukungan email"]'::jsonb
        WHERE id = '{TRIAL_ID}'
        """
    )
    op.execute(
        f"""
        UPDATE subscription_plans
        SET label = 'Growth', max_agents = 5, is_active = true, monthly_price_idr = 299000,
            monthly_message_limit = 10000,
            marketing_features = '["5 asisten AI", "10.000 pesan / bulan", "Integrasi dokumen", "Dukungan prioritas"]'::jsonb
        WHERE id = '{GROWTH_ID}'
        """
    )
    op.execute(
        f"""
        UPDATE subscription_plans
        SET label = 'Business', max_agents = NULL, period_days = 30, grace_period_days = 3,
            is_active = true, monthly_price_idr = 999000,
            monthly_message_limit = NULL,
            marketing_features = '["Asisten tak terbatas", "Pesan tak terbatas", "SLA & onboarding"]'::jsonb
        WHERE id = '{BUSINESS_ID}'
        """
    )
    # Existing Pro subscribers become Growth; their subscription status and
    # expiry remain unchanged.  The legacy plan is then hidden from the API.
    op.execute(
        f"UPDATE user_subscriptions SET plan_id = '{GROWTH_ID}' WHERE plan_id = '{LEGACY_PRO_ID}'"
    )
    op.execute(
        f"""
        UPDATE user_subscriptions
        SET expires_at = COALESCE(expires_at, now() + INTERVAL '30 days'),
            grace_until = COALESCE(grace_until, now() + INTERVAL '33 days')
        WHERE plan_id = '{BUSINESS_ID}' AND status IN ('active', 'grace_period')
        """
    )
    op.execute(
        f"UPDATE subscription_plans SET is_active = false WHERE id = '{LEGACY_PRO_ID}'"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE subscription_plans SET is_active = true WHERE id = '{LEGACY_PRO_ID}'"
    )
    op.drop_column("subscription_plans", "marketing_features")
    op.drop_column("subscription_plans", "monthly_message_limit")
    op.drop_column("subscription_plans", "monthly_price_idr")
