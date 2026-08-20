"""Persistent Arthur build state for restart-safe, idempotent workflows."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentBuildDraft(Base):
    __tablename__ = "agent_build_drafts"
    __table_args__ = (
        Index("ix_agent_build_drafts_owner_updated", "owner_external_id", "updated_at"),
        Index("ix_agent_build_drafts_session_updated", "session_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    target_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    target_agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    intent: Mapped[str] = mapped_column(String(64), nullable=False, default="discover")
    workflow_state: Mapped[str] = mapped_column(String(64), nullable=False, default="discovery")
    facts_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    evidence_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    question_history_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    required_integrations_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    integration_status_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    artifact_status_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    confirmation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    idempotency_keys_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    skill_versions_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False, default="arthur-legacy")
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_inbound_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
