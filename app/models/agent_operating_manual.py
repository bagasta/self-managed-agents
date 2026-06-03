import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentOperatingManual(Base):
    __tablename__ = "agent_operating_manuals"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_operating_manual_agent_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="arthur_generic")
    domain: Mapped[str] = mapped_column(String(128), nullable=False, default="generic")
    domain_confidence: Mapped[str] = mapped_column(String(32), nullable=False, default="low")
    maturity: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    owner_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    missing_context: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    assumptions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    workflows: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
