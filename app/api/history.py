import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import verify_api_key
from app.models.message import Message
from app.models.session import Session
from app.schemas.message import HistoryMessage, HistoryResponse

router = APIRouter(prefix="/v1/sessions", tags=["history"])


@router.get("/{session_id}/history", response_model=HistoryResponse)
async def get_history(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> HistoryResponse:
    session = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    rows = (
        await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.step_index, Message.timestamp)
        )
    ).scalars().all()

    return HistoryResponse(
        session_id=session_id,
        messages=[HistoryMessage.model_validate(r) for r in rows],
    )
