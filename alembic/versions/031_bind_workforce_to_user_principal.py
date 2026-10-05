"""Bind workforce ownership to stable User UUIDs.

Revision ID: 031
Revises: 030
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


_BACKFILL = """
WITH matches AS (
    SELECT source.id AS source_id, array_agg(DISTINCT users.id) AS user_ids
    FROM {table} AS source
    JOIN users ON (
        users.external_id = source.{identity_column}
        OR users.phone_number = source.{identity_column}
        OR users.wa_lid = source.{identity_column}
        OR (
            source.{identity_column} ~ '^\\+?[0-9]+$'
            AND users.external_id ~ '^\\+?[0-9]+$'
            AND ltrim(source.{identity_column}, '+') = ltrim(users.external_id, '+')
        )
        OR (
            source.{identity_column} ~ '^\\+?[0-9]+$'
            AND users.phone_number ~ '^\\+?[0-9]+$'
            AND ltrim(source.{identity_column}, '+') = ltrim(users.phone_number, '+')
        )
    )
    WHERE source.{identity_column} IS NOT NULL
    GROUP BY source.id
    HAVING count(DISTINCT users.id) = 1
)
UPDATE {table} AS source
SET {principal_column} = matches.user_ids[1]
FROM matches
WHERE source.id = matches.source_id
"""


def upgrade() -> None:
    for table in ("agents", "user_api_keys", "workforce_tasks"):
        op.add_column(
            table,
            sa.Column(
                "owner_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_owner_user_id", table, ["owner_user_id"])

    op.add_column(
        "workforce_task_steps",
        sa.Column(
            "parent_step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workforce_task_steps.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_workforce_task_steps_parent_step_id", "workforce_task_steps", ["parent_step_id"]
    )

    # Only unambiguous exact/plus-normalized phone matches are carried forward. Rows
    # with missing or conflicting User aliases remain NULL and owner-path reads
    # exclude them until explicitly rebound by a trusted authenticated flow.
    for table, identity_column in (
        ("agents", "owner_external_id"),
        ("user_api_keys", "owner_external_id"),
        ("workforce_tasks", "workspace_id"),
    ):
        op.execute(
            sa.text(
                _BACKFILL.format(
                    table=table,
                    identity_column=identity_column,
                    principal_column="owner_user_id",
                )
            )
        )


def downgrade() -> None:
    op.drop_index("ix_workforce_task_steps_parent_step_id", table_name="workforce_task_steps")
    op.drop_column("workforce_task_steps", "parent_step_id")
    for table in ("workforce_tasks", "user_api_keys", "agents"):
        op.drop_index(f"ix_{table}_owner_user_id", table_name=table)
        op.drop_column(table, "owner_user_id")
