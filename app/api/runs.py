import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import verify_api_key
from app.models.message import Message
from app.models.run import Run
from app.schemas.message import HistoryMessage

router = APIRouter(prefix="/v1/runs", tags=["runs"])


class RunDetailResponse(BaseModel):
    run_id: uuid.UUID
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    openrouter_cost_usd: float = 0.0
    usage_details: list[dict] | None = None
    messages: list[HistoryMessage]


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> RunDetailResponse:
    # Try Run record first (new path)
    run_row = (
        await db.execute(select(Run).where(Run.id == run_id))
    ).scalar_one_or_none()

    rows = (
        await db.execute(
            select(Message)
            .where(Message.run_id == run_id)
            .order_by(Message.step_index, Message.timestamp)
        )
    ).scalars().all()

    if not run_row and not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    return RunDetailResponse(
        run_id=run_id,
        status=run_row.status if run_row else "unknown",
        started_at=run_row.started_at if run_row else None,
        completed_at=run_row.completed_at if run_row else None,
        error_message=run_row.error_message if run_row else None,
        tokens_used=run_row.tokens_used if run_row else 0,
        prompt_tokens=run_row.prompt_tokens if run_row else 0,
        completion_tokens=run_row.completion_tokens if run_row else 0,
        reasoning_tokens=run_row.reasoning_tokens if run_row else 0,
        cached_tokens=run_row.cached_tokens if run_row else 0,
        openrouter_cost_usd=float(run_row.openrouter_cost_usd or 0) if run_row else 0.0,
        usage_details=run_row.usage_details if run_row else None,
        messages=[HistoryMessage.model_validate(r) for r in rows],
    )
