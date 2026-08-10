"""conversation_summaries table — production-ready conversation memory storage.

Revision ID: 025
Revises: 024
Create Date: 2026-08-05

Replaces the anti-pattern of storing LLM-generated context summaries as a blob
inside session.metadata_["context_summary"]. Instead, summaries are stored in a
dedicated, indexed, queryable table with full lifecycle management.

Why this matters:
  - JSONB blobs can't be efficiently indexed or queried
  - A dedicated table supports versioning, expiry, and audit history
  - Partial unique index enforces "one active summary per session" at DB level
  - Enables future Redis caching with proper cache invalidation keys

Backfill: Migrates any existing session.metadata_["context_summary"] values
          into the new table so no summary data is lost on upgrade.
"""
from __future__ import annotations

import json
import uuid as _uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: Union[str, Sequence[str], None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 1. Create conversation_summaries table                              #
    # ------------------------------------------------------------------ #
    if not _has_table("conversation_summaries"):
        op.create_table(
            "conversation_summaries",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "session_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("summary_text", sa.Text(), nullable=False),
            sa.Column("message_count_at", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    # Composite indexes for fast lookups
    if not _has_index("conversation_summaries", "ix_conv_summaries_session_active"):
        op.create_index(
            "ix_conv_summaries_session_active",
            "conversation_summaries",
            ["session_id", "is_active"],
        )
    if not _has_index("conversation_summaries", "ix_conv_summaries_session_created"):
        op.create_index(
            "ix_conv_summaries_session_created",
            "conversation_summaries",
            ["session_id", "created_at"],
        )

    # Partial unique index: only one active summary per session.
    # SQLAlchemy ORM can't express partial unique indexes in __table_args__,
    # so we create it here with raw SQL.
    conn = op.get_bind()
    existing_indexes = [
        idx["name"]
        for idx in sa.inspect(conn).get_indexes("conversation_summaries")
    ]
    if "uix_conv_summaries_session_one_active" not in existing_indexes:
        conn.execute(sa.text(
            "CREATE UNIQUE INDEX uix_conv_summaries_session_one_active "
            "ON conversation_summaries (session_id) "
            "WHERE is_active = TRUE"
        ))

    # ------------------------------------------------------------------ #
    # 2. Backfill: migrate existing summaries from session.metadata_      #
    # ------------------------------------------------------------------ #
    # Read all sessions that have a stored context_summary in their JSONB metadata.
    # Insert one ConversationSummary row per session so no summary data is lost.
    result = conn.execute(sa.text(
        "SELECT id, metadata "
        "FROM sessions "
        "WHERE metadata IS NOT NULL "
        "  AND metadata->>'context_summary' IS NOT NULL "
        "  AND metadata->>'context_summary' <> ''"
    ))
    rows = result.fetchall()

    now = datetime.now(timezone.utc)
    for session_id, metadata in rows:
        meta: dict = metadata if isinstance(metadata, dict) else json.loads(metadata or "{}")
        summary = meta.get("context_summary", "")
        msg_count = int(meta.get("context_summary_at_msg", 0))
        if not summary:
            continue
        # Estimate tokens: rough heuristic (1 token ≈ 4 chars for English/Indonesian)
        token_estimate = max(1, len(summary) // 4)
        conn.execute(sa.text(
            "INSERT INTO conversation_summaries "
            "(id, session_id, summary_text, message_count_at, token_estimate, is_active, created_at) "
            "VALUES (:id, :session_id, :summary_text, :message_count_at, :token_estimate, TRUE, :created_at) "
            "ON CONFLICT DO NOTHING"
        ), {
            "id": str(_uuid.uuid4()),
            "session_id": str(session_id),
            "summary_text": summary,
            "message_count_at": msg_count,
            "token_estimate": token_estimate,
            "created_at": now,
        })


def downgrade() -> None:
    # Drop indexes first, then the table
    conn = op.get_bind()
    if _has_table("conversation_summaries"):
        existing_indexes = [
            idx["name"]
            for idx in sa.inspect(conn).get_indexes("conversation_summaries")
        ]
        if "uix_conv_summaries_session_one_active" in existing_indexes:
            conn.execute(sa.text(
                "DROP INDEX IF EXISTS uix_conv_summaries_session_one_active"
            ))
        if _has_index("conversation_summaries", "ix_conv_summaries_session_active"):
            op.drop_index("ix_conv_summaries_session_active", table_name="conversation_summaries")
        if _has_index("conversation_summaries", "ix_conv_summaries_session_created"):
            op.drop_index("ix_conv_summaries_session_created", table_name="conversation_summaries")
        op.drop_table("conversation_summaries")
