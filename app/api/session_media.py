"""Signed attachment transfer endpoint for the existing Google upload tool."""
import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.agent import Agent
from app.models.session import Session
from app.core.infra.session_media_transfer import decode_session_media_token, read_session_media_transfer

router = APIRouter()


@router.get("/v1/session-media/{token}", include_in_schema=False)
async def download_session_media(token: str, db: AsyncSession = Depends(get_db)):
    try:
        claims = decode_session_media_token(token)
        session = await db.get(Session, uuid.UUID(claims["sid"]))
        agent = await db.get(Agent, uuid.UUID(claims["aid"]))
        if session is None or agent is None or agent.is_deleted:
            raise ValueError("Attachment is unavailable")
        data, entry = await asyncio.to_thread(read_session_media_transfer, session, claims)
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="Attachment unavailable") from None
    mime = str(entry.get("mimetype") or "application/octet-stream").split(";", 1)[0]
    return Response(data, media_type=mime, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
