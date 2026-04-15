import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_runner import run_agent
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent
from app.models.session import Session
from app.schemas.message import MessageCreate, MessageResponse, StepSummary

router = APIRouter(prefix="/v1/agents", tags=["messages"])


@router.post(
    "/{agent_id}/sessions/{session_id}/messages",
    response_model=MessageResponse,
)
async def send_message(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: MessageCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> MessageResponse:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )

    session = (
        await db.execute(
            select(Session).where(
                Session.id == session_id, Session.agent_id == agent_id
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found for agent {agent_id}",
        )

    result = await run_agent(
        agent_model=agent,
        session=session,
        user_message=payload.message,
        db=db,
    )

    return MessageResponse(
        reply=result["reply"],
        steps=[StepSummary(**s) for s in result["steps"]],
        run_id=result["run_id"],
    )
