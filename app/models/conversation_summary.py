"""
ConversationSummary model — persists LLM-generated session summaries in PostgreSQL.

Replaces the legacy pattern of storing summaries inside session.metadata_["context_summary"]
(an opaque JSONB blob). A dedicated table gives us:
  - Indexable queries (fetch the active summary for a session in O(log n))
  - Full lifecycle management (version history, expiry, invalidation)
  - Clean audit trail for debugging context window issues
  - Backward-compatible: session.metadata_ is NOT touched during transition
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ConversationSummary(Base):
    """
    Stores a single LLM-generated summary of a conversation session.

    Lifecycle:
        1. `maybe_summarize_context()` triggers every `context_summary_trigger` user messages.
        2. A new ConversationSummary row is inserted with `is_active=True`.
        3. All previous summaries for the same session are marked `is_active=False`.
        4. When loading context, only the single active summary is fetched (indexed lookup).
        5. Old summaries are retained for audit / rollback but not loaded into LLM context.

    Why one active summary per session?
        - An agent only needs the most recent distillation of a conversation.
        - Keeping history lets us roll back or debug context drift.
        - The partial unique index enforces the invariant at the DB level.
    """

    __tablename__ = "conversation_summaries"
    __table_args__ = (
        # Fast lookup: get the active summary for a session
        Index("ix_conv_summaries_session_active", "session_id", "is_active"),
        # Ordered history: list all summaries for a session newest-first
        Index("ix_conv_summaries_session_created", "session_id", "created_at"),
        # Enforce uniqueness: only one active summary per session (DB-level invariant)
        # This is a partial unique index — only rows where is_active=True are considered.
        # SQLAlchemy doesn't support partial unique indexes via __table_args__ directly,
        # so this is created by raw SQL in the Alembic migration instead.
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The actual Markdown-formatted summary text injected into the LLM system prompt.
    # Generated dynamically — never read from or written to disk.
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Number of user messages in the session when this summary was generated.
    # Used to decide when the summary is stale and needs regeneration.
    message_count_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Approximate token count of the summary text (estimated, not guaranteed exact).
    # Useful for context-window budget management.
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Only one summary per session is active at any time.
    # Older summaries are kept as history but excluded from context loading.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )

    # Optional: TTL-based cleanup. NULL = keep forever.
    # A background job can DELETE WHERE expires_at < NOW() AND is_active = FALSE.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
