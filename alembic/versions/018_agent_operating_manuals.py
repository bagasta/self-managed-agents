"""add agent operating manuals

Revision ID: 018
Revises: 017
Create Date: 2026-06-02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "018"
down_revision: Union[str, Sequence[str], None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("agent_operating_manuals"):
        op.create_table(
            "agent_operating_manuals",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("agent_id", sa.UUID(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source", sa.String(length=64), nullable=False, server_default="arthur_generic"),
            sa.Column("domain", sa.String(length=128), nullable=False, server_default="generic"),
            sa.Column("domain_confidence", sa.String(length=32), nullable=False, server_default="low"),
            sa.Column("maturity", sa.String(length=32), nullable=False, server_default="draft"),
            sa.Column("owner_review_required", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("missing_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
            sa.Column("assumptions", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
            sa.Column("workflows", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
            sa.Column("created_by_agent_id", sa.String(length=64), nullable=True),
            sa.Column("reviewed_by", sa.String(length=64), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("agent_id", "version", name="uq_agent_operating_manual_agent_version"),
        )
        op.create_index(
            "ix_agent_operating_manuals_agent_id",
            "agent_operating_manuals",
            ["agent_id"],
            unique=False,
        )
        op.create_index(
            "ix_agent_operating_manuals_maturity",
            "agent_operating_manuals",
            ["maturity"],
            unique=False,
        )

    if _has_table("agents"):
        op.execute(
            """
            INSERT INTO agent_operating_manuals (
                id,
                agent_id,
                version,
                source,
                domain,
                domain_confidence,
                maturity,
                owner_review_required,
                missing_context,
                assumptions,
                workflows,
                created_by_agent_id
            )
            SELECT
                gen_random_uuid(),
                a.id,
                COALESCE(NULLIF((a.tools_config->'operating_manual'->>'version'), '')::integer, 1),
                COALESCE(a.tools_config->'operating_manual'->>'source', 'arthur_generic'),
                COALESCE(a.tools_config->'operating_manual'->>'domain', 'generic'),
                COALESCE(a.tools_config->'operating_manual'->>'domain_confidence', 'low'),
                COALESCE(a.tools_config->'operating_manual'->>'maturity', 'draft'),
                COALESCE((a.tools_config->'operating_manual'->>'owner_review_required')::boolean, true),
                COALESCE(a.tools_config->'operating_manual'->'missing_context', '[]'::jsonb),
                COALESCE(a.tools_config->'operating_manual'->'assumptions', '[]'::jsonb),
                COALESCE(a.tools_config->'operating_manual'->'workflows', '[]'::jsonb),
                a.created_by_agent_id
            FROM agents a
            WHERE a.tools_config ? 'operating_manual'
              AND jsonb_typeof(a.tools_config->'operating_manual') = 'object'
              AND NOT EXISTS (
                  SELECT 1
                  FROM agent_operating_manuals m
                  WHERE m.agent_id = a.id
                    AND m.version = COALESCE(NULLIF((a.tools_config->'operating_manual'->>'version'), '')::integer, 1)
              )
            """
        )


def downgrade() -> None:
    if _has_table("agent_operating_manuals"):
        op.drop_index("ix_agent_operating_manuals_maturity", table_name="agent_operating_manuals")
        op.drop_index("ix_agent_operating_manuals_agent_id", table_name="agent_operating_manuals")
        op.drop_table("agent_operating_manuals")
