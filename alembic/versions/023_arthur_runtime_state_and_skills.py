"""Arthur persistent build state, versioned skills, and run metadata.

Revision ID: 023
Revises: 022
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("skills", sa.Column("version", sa.String(32), nullable=False, server_default="user"))
    op.add_column("skills", sa.Column("triggers", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column("skills", sa.Column("supported_states", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column("skills", sa.Column("allowed_tool_groups", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column("skills", sa.Column("checksum", sa.String(64), nullable=True))
    op.add_column("skills", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("skills", sa.Column("trust_level", sa.String(32), nullable=False, server_default="user"))
    op.add_column("skills", sa.Column("bundle_version", sa.String(64), nullable=True))
    op.add_column("skills", sa.Column("immutable", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("skills", sa.Column("publisher", sa.String(255), nullable=True))
    op.add_column("skills", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("uq_agent_skill_name", "skills", type_="unique")
    op.create_unique_constraint("uq_agent_skill_name_version", "skills", ["agent_id", "name", "version"])

    op.add_column(
        "runs",
        sa.Column("runtime_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
    )

    op.create_table(
        "agent_build_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_external_id", sa.String(255), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_agent_name", sa.String(255), nullable=True),
        sa.Column("intent", sa.String(64), nullable=False, server_default="discover"),
        sa.Column("workflow_state", sa.String(64), nullable=False, server_default="discovery"),
        sa.Column("facts_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("evidence_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("question_history_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("required_integrations_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("integration_status_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("artifact_status_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("confirmation_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("idempotency_keys_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("prompt_version", sa.String(64), nullable=False, server_default="unknown"),
        sa.Column("skill_versions_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("engine_version", sa.String(64), nullable=False, server_default="arthur-legacy"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_inbound_message_id", sa.String(255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_agent_id"], ["agents.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_agent_build_drafts_owner_updated",
        "agent_build_drafts",
        ["owner_external_id", "updated_at"],
    )
    op.create_index(
        "ix_agent_build_drafts_session_updated",
        "agent_build_drafts",
        ["session_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_build_drafts_session_updated", table_name="agent_build_drafts")
    op.drop_index("ix_agent_build_drafts_owner_updated", table_name="agent_build_drafts")
    op.drop_table("agent_build_drafts")
    op.drop_column("runs", "runtime_metadata")
    op.drop_constraint("uq_agent_skill_name_version", "skills", type_="unique")
    # A versioned bundle may contain multiple rows for one agent/name. Keep the
    # newest enabled version (or newest version if all are disabled) before
    # restoring the pre-023 uniqueness contract.
    op.execute(
        sa.text(
            """
            DELETE FROM skills
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        row_number() OVER (
                            PARTITION BY agent_id, name
                            ORDER BY enabled DESC, published_at DESC NULLS LAST,
                                     updated_at DESC, created_at DESC, id DESC
                        ) AS row_rank
                    FROM skills
                ) ranked
                WHERE row_rank > 1
            )
            """
        )
    )
    op.create_unique_constraint("uq_agent_skill_name", "skills", ["agent_id", "name"])
    for column in (
        "published_at",
        "publisher",
        "immutable",
        "bundle_version",
        "trust_level",
        "enabled",
        "checksum",
        "allowed_tool_groups",
        "supported_states",
        "triggers",
        "version",
    ):
        op.drop_column("skills", column)
