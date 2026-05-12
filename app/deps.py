from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.user_api_key import hash_user_key

settings = get_settings()


async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> str:
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return x_api_key


async def verify_user_key(
    x_user_key: str = Header(..., alias="X-User-Key"),
    db: AsyncSession = Depends(get_db),
) -> "UserApiKey":
    from app.models.user_api_key import UserApiKey

    row = (
        await db.execute(select(UserApiKey).where(UserApiKey.key_hash == hash_user_key(x_user_key)))
    ).scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if row.revoked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key has been revoked")
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key has expired")
    return row
