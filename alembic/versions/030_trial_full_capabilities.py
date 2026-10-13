"""Make every subscription tier feature-equivalent.

Revision ID: 030
Revises: 029
"""
from __future__ import annotations

from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE subscription_plans
        SET allowed_models = '[]'::jsonb
        WHERE code IN ('trial', 'tier_1', 'tier_3')
        """
    )
    op.execute(
        """
        UPDATE subscription_plans
        SET marketing_features = CASE code
            WHEN 'trial' THEN '["1 asisten AI", "500 pesan / bulan", "Semua kemampuan termasuk integrasi Google", "Dukungan email"]'::jsonb
            WHEN 'tier_1' THEN '["5 asisten AI", "10.000 pesan / bulan", "Semua kemampuan termasuk integrasi Google", "Dukungan prioritas"]'::jsonb
            WHEN 'tier_3' THEN '["Asisten tak terbatas", "Pesan tak terbatas", "Semua kemampuan termasuk integrasi Google", "SLA & onboarding"]'::jsonb
        END
        WHERE code IN ('trial', 'tier_1', 'tier_3')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE subscription_plans
        SET allowed_models = '["deepseek/deepseek-v4-flash", "openai/gpt-4.1-mini"]'::jsonb
        WHERE code IN ('trial', 'tier_1')
        """
    )
