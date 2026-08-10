"""User API key management endpoints.

- POST /v1/auth/keys          — generate a new user key (admin only)
- GET  /v1/auth/keys/me       — check status of a user key
- POST /v1/auth/keys/renew    — renew (extend + un-revoke) a user key
- POST /v1/auth/keys/{id}/revoke — revoke a user key (admin only)
"""
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import verify_api_key, verify_user_key
from app.models.user_api_key import (
    UserApiKey,
    _DEFAULT_KEY_TTL_DAYS,
    generate_user_key,
    hash_user_key,
)
from app.schemas.user_api_key import (
    UserApiKeyCreate,
    UserApiKeyCreateResponse,
    UserApiKeyRenewResponse,
    UserApiKeyStatusResponse,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/auth/keys", tags=["auth"])


@router.post("", response_model=UserApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def generate_key(
    payload: UserApiKeyCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> UserApiKeyCreateResponse:
    """Generate a new user API key. Admin-only (requires X-API-Key)."""
    raw_key = generate_user_key()
    key = UserApiKey(label=payload.label, key_hash=hash_user_key(raw_key))
    db.add(key)
    await db.flush()
    await db.refresh(key)
    logger.info("auth.key.created", key_id=str(key.id), label=key.label)
    return UserApiKeyCreateResponse(
        id=key.id,
        key=raw_key,
        label=key.label,
        expires_at=key.expires_at,
        revoked=key.revoked,
        created_at=key.created_at,
    )


@router.get("/me", response_model=UserApiKeyStatusResponse)
async def get_key_status(
    current_key: UserApiKey = Depends(verify_user_key),
) -> UserApiKeyStatusResponse:
    """Return status of the caller's user key (requires X-User-Key)."""
    return UserApiKeyStatusResponse.model_validate(current_key)


@router.post("/renew", response_model=UserApiKeyRenewResponse)
async def renew_key(
    x_user_key: str = Header(..., alias="X-User-Key"),
    db: AsyncSession = Depends(get_db),
) -> UserApiKeyRenewResponse:
    """Extend a user key by another 30 days from now (requires X-User-Key)."""
    key_obj = (
        await db.execute(select(UserApiKey).where(UserApiKey.key_hash == hash_user_key(x_user_key)))
    ).scalar_one_or_none()
    if key_obj is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if key_obj.revoked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key has been revoked")
    expires = key_obj.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key has expired")

    key_obj.expires_at = datetime.now(timezone.utc) + timedelta(days=_DEFAULT_KEY_TTL_DAYS)
    key_obj.revoked = False
    await db.flush()
    await db.refresh(key_obj)
    logger.info("auth.key.renewed", key_id=str(key_obj.id))
    new_expires = key_obj.expires_at
    if new_expires.tzinfo is None:
        new_expires = new_expires.replace(tzinfo=timezone.utc)
    return UserApiKeyRenewResponse(
        id=key_obj.id,
        label=key_obj.label,
        expires_at=key_obj.expires_at,
        revoked=key_obj.revoked,
        created_at=key_obj.created_at,
        message=f"Key renewed for {_DEFAULT_KEY_TTL_DAYS} days. Expires {new_expires.date()}.",
    )


@router.post("/{key_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> None:
    """Revoke a user key by ID. Admin-only (requires X-API-Key)."""
    row = (
        await db.execute(select(UserApiKey).where(UserApiKey.id == key_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
    row.revoked = True
    await db.flush()
    logger.info("auth.key.revoked", key_id=str(key_id))
