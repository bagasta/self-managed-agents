"""subscription_system

Revision ID: 013
Revises: 012
Create Date: 2026-05-11

Creates:
  - users
  - subscription_plans  (+ seed data)
  - user_subscriptions
  - token_topups
  - agents.owner_external_id  (FK link ke users)
"""
from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("has_used_trial", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_external_id", "users", ["external_id"], unique=True)

    # ------------------------------------------------------------------
    # 2. subscription_plans
    # ------------------------------------------------------------------
    op.create_table(
        "subscription_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("max_agents", sa.Integer(), nullable=True),          # NULL = unlimited (Enterprise)
        sa.Column("token_quota", sa.BigInteger(), nullable=False),     # 0 = custom/kontrak
        sa.Column("period_days", sa.Integer(), nullable=True),         # NULL = trial (tidak ada expiry)
        sa.Column("grace_period_days", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("allowed_models", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("subagents_allowed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("wa_connect", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_trial", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_subscription_plans_code", "subscription_plans", ["code"], unique=True)

    # ------------------------------------------------------------------
    # 3. Seed subscription_plans
    # ------------------------------------------------------------------
    plans_table = sa.table(
        "subscription_plans",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("max_agents", sa.Integer),
        sa.column("token_quota", sa.BigInteger),
        sa.column("period_days", sa.Integer),
        sa.column("grace_period_days", sa.Integer),
        sa.column("allowed_models", postgresql.JSONB),
        sa.column("subagents_allowed", sa.Boolean),
        sa.column("wa_connect", sa.Boolean),
        sa.column("is_trial", sa.Boolean),
        sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(plans_table, [
        {
            "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
            "code": "trial",
            "label": "Trial",
            "max_agents": 1,
            "token_quota": 2_000_000,
            "period_days": None,
            "grace_period_days": 3,
            "allowed_models": ["openai/gpt-4.1-mini"],
            "subagents_allowed": False,
            "wa_connect": True,
            "is_trial": True,
            "is_active": True,
        },
        {
            "id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
            "code": "tier_1",
            "label": "Starter",
            "max_agents": 1,
            "token_quota": 10_000_000,
            "period_days": 30,
            "grace_period_days": 3,
            "allowed_models": ["openai/gpt-4.1-mini"],
            "subagents_allowed": True,
            "wa_connect": True,
            "is_trial": False,
            "is_active": True,
        },
        {
            "id": uuid.UUID("00000000-0000-0000-0000-000000000003"),
            "code": "tier_2",
            "label": "Pro",
            "max_agents": 2,
            "token_quota": 20_000_000,
            "period_days": 30,
            "grace_period_days": 3,
            "allowed_models": ["openai/gpt-4.1-mini", "deepseek/deepseek-v4-flash"],
            "subagents_allowed": True,
            "wa_connect": True,
            "is_trial": False,
            "is_active": True,
        },
        {
            "id": uuid.UUID("00000000-0000-0000-0000-000000000004"),
            "code": "tier_3",
            "label": "Enterprise",
            "max_agents": None,
            "token_quota": 0,
            "period_days": None,
            "grace_period_days": 7,
            "allowed_models": [],
            "subagents_allowed": True,
            "wa_connect": True,
            "is_trial": False,
            "is_active": True,
        },
    ])

    # ------------------------------------------------------------------
    # 4. user_subscriptions
    # ------------------------------------------------------------------
    op.create_table(
        "user_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="trial",
            # "trial" | "active" | "grace_period" | "expired"
        ),
        sa.Column("token_quota", sa.BigInteger(), nullable=False),
        sa.Column("tokens_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["subscription_plans.id"]),
    )
    op.create_index("ix_user_subscriptions_user_id", "user_subscriptions", ["user_id"], unique=True)
    op.create_index("ix_user_subscriptions_status", "user_subscriptions", ["status"])

    # ------------------------------------------------------------------
    # 5. token_topups
    # ------------------------------------------------------------------
    op.create_table(
        "token_topups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tokens_added", sa.BigInteger(), nullable=False),
        sa.Column("token_quota_before", sa.BigInteger(), nullable=False),
        sa.Column("token_quota_after", sa.BigInteger(), nullable=False),
        sa.Column("reference_id", sa.String(255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("topped_up_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["user_subscriptions.id"]),
    )
    op.create_index("ix_token_topups_reference_id", "token_topups", ["reference_id"], unique=True)
    op.create_index("ix_token_topups_user_id", "token_topups", ["user_id"])

    # ------------------------------------------------------------------
    # 6. agents.owner_external_id  (link ke users.external_id)
    # ------------------------------------------------------------------
    op.add_column("agents", sa.Column("owner_external_id", sa.String(64), nullable=True))
    op.create_index("ix_agents_owner_external_id", "agents", ["owner_external_id"])


def downgrade() -> None:
    op.drop_index("ix_agents_owner_external_id", table_name="agents")
    op.drop_column("agents", "owner_external_id")

    op.drop_index("ix_token_topups_user_id", table_name="token_topups")
    op.drop_index("ix_token_topups_reference_id", table_name="token_topups")
    op.drop_table("token_topups")

    op.drop_index("ix_user_subscriptions_status", table_name="user_subscriptions")
    op.drop_index("ix_user_subscriptions_user_id", table_name="user_subscriptions")
    op.drop_table("user_subscriptions")

    op.drop_index("ix_subscription_plans_code", table_name="subscription_plans")
    op.drop_table("subscription_plans")

    op.drop_index("ix_users_external_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
