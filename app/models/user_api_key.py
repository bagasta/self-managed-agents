import secrets
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy import Boolean, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

_DEFAULT_KEY_TTL_DAYS = 30


def generate_user_key() -> str:
    return "uak_" + secrets.token_urlsafe(32)


def hash_user_key(raw_key: str) -> str:
    return sha256(raw_key.encode("utf-8")).hexdigest()


def _default_expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=_DEFAULT_KEY_TTL_DAYS)


class UserApiKey(Base):
    __tablename__ = "user_api_keys"
    __table_args__ = (Index("ix_user_api_keys_key_hash", "key_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_default_expires_at
    )
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def is_active(self) -> bool:
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return not self.revoked and datetime.now(timezone.utc) < expires
