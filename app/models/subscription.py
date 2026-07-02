import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # WhatsApp LID alias (ID internal WA, bukan nomor asli). Jangan pernah
    # simpan LID di phone_number; lookup subscription mencocokkan kedua kolom.
    wa_lid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    has_used_trial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    subscription: Mapped["UserSubscription | None"] = relationship("UserSubscription", back_populates="user", uselist=False)


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    max_agents: Mapped[int | None] = mapped_column(Integer, nullable=True)       # NULL = unlimited
    token_quota: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)      # NULL = trial
    grace_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    allowed_models: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    subagents_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    wa_connect: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Fixed UUIDs untuk lookup by code tanpa query (dipakai di kode platform)
    TRIAL_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
    TIER_1_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
    TIER_2_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
    TIER_3_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


class UserSubscription(Base):
    __tablename__ = "user_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("subscription_plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="trial")
    # "trial" | "active" | "grace_period" | "expired"
    token_quota: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tokens_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="subscription")
    plan: Mapped["SubscriptionPlan"] = relationship("SubscriptionPlan")
    topups: Mapped[list["TokenTopup"]] = relationship("TokenTopup", back_populates="subscription")

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.token_quota - self.tokens_used)

    @property
    def is_usable(self) -> bool:
        """True jika masih bisa dipakai (trial/active/grace_period)."""
        return self.status in ("trial", "active", "grace_period")


class TokenTopup(Base):
    __tablename__ = "token_topups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("user_subscriptions.id"), nullable=False)
    tokens_added: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_quota_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_quota_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_id: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    topped_up_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    subscription: Mapped["UserSubscription"] = relationship("UserSubscription", back_populates="topups")
