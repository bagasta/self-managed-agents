import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OutboundMessage(Base):
    """A durable, per-recipient WhatsApp delivery queue item."""

    __tablename__ = "outbound_messages"
    __table_args__ = (
        Index("ix_outbound_messages_due", "status", "available_at"),
        Index("ix_outbound_messages_target", "agent_id", "target", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    source_device_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # queued | sending | sent | cancelled | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    # reply_to_customer is retained for audit and future prioritisation.
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="reply_to_customer")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
