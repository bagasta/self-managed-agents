"""Durable internal work records for the workforce control plane.

These records describe work assigned between existing agents.  They do not
execute an agent, deliver a channel message, or grant a tool permission.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorkforceTask(Base):
    """A workspace-owned assignment which can outlive a chat session or run."""

    __tablename__ = "workforce_tasks"
    __table_args__ = (
        Index("ix_workforce_tasks_workspace_status", "workspace_id", "status"),
        Index("ix_workforce_tasks_workspace_assignee", "workspace_id", "assigned_agent_id"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_workforce_tasks_workspace_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The existing platform's tenant identity is the owner external ID.  This
    # deliberately avoids inventing a second account model in the MVP.
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # draft | open | in_progress | blocked | completed | cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", server_default="open")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal", server_default="normal")
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workforce_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    customer_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkforceTaskStep(Base):
    """One bounded, internal unit of work for a workforce task."""

    __tablename__ = "workforce_task_steps"
    __table_args__ = (
        Index("ix_workforce_task_steps_task_status", "task_id", "status"),
        UniqueConstraint("task_id", "sequence", name="uq_workforce_task_steps_task_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workforce_tasks.id", ondelete="CASCADE"), nullable=False
    )
    parent_step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workforce_task_steps.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending | in_progress | blocked | completed | cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    input_context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    delegated_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkforceTaskEvent(Base):
    """Append-only audit event.  Event metadata must never contain credentials."""

    __tablename__ = "workforce_task_events"
    __table_args__ = (Index("ix_workforce_task_events_task_created", "task_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workforce_tasks.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workforce_task_steps.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    actor_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="workspace", server_default="workspace")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
