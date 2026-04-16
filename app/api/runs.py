import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import verify_api_key
from app.models.message import Message
from app.schemas.message import HistoryMessage

router = APIRouter(prefix="/v1/runs", tags=["runs"])


class RunResponse:
    pass


from pydantic import BaseModel


class RunDetailResponse(BaseModel):
    run_id: uuid.UUID
    messages: list[HistoryMessage]


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> RunDetailResponse:
    rows = (
        await db.execute(
            select(Message)
            .where(Message.run_id == run_id)
            .order_by(Message.step_index, Message.timestamp)
        )
    ).scalars().all()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    return RunDetailResponse(
        run_id=run_id,
        messages=[HistoryMessage.model_validate(r) for r in rows],
    )
