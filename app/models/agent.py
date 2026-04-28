import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

_DEFAULT_TOKEN_QUOTA = 4_000_000
_DEFAULT_PERIOD_DAYS = 30


def _default_active_until() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=_DEFAULT_PERIOD_DAYS)


def _generate_api_key() -> str:
    return secrets.token_urlsafe(32)


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        Index("ix_agents_api_key", "api_key", unique=True),
        Index("ix_agents_wa_device_id", "wa_device_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(
        String(255), nullable=False, default="anthropic/claude-sonnet-4-6"
    )
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    tools_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sandbox_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    safety_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    escalation_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- whatsapp channel ---
    wa_device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- operator access ---
    operator_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # --- allowlist ---
    # null = semua nomor diizinkan (default)
    # ["628111", "628222"] = hanya nomor ini yang dibalas (non-operator)
    allowed_senders: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # --- subscription / quota ---
    api_key: Mapped[str] = mapped_column(
        String(64), nullable=False, default=_generate_api_key
    )
    token_quota: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_DEFAULT_TOKEN_QUOTA
    )
    tokens_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    active_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_default_active_until
    )
    quota_period_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=_DEFAULT_PERIOD_DAYS
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
