import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_runner import run_agent
from app.database import get_db
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
    x_agent_key: str = Header(..., alias="X-Agent-Key"),
    db: AsyncSession = Depends(get_db),
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

    if agent.api_key != x_agent_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent API key",
        )

    now = datetime.now(timezone.utc)
    active_until = agent.active_until
    if active_until.tzinfo is None:
        active_until = active_until.replace(tzinfo=timezone.utc)
    if active_until < now:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Agent subscription expired on {agent.active_until.isoformat()}. "
                "Call POST /v1/agents/{agent_id}/renew to reactivate."
            ),
        )

    if agent.tokens_used >= agent.token_quota:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Token quota exhausted ({agent.tokens_used:,} / {agent.token_quota:,}). "
                "Call POST /v1/agents/{agent_id}/renew to reset quota."
            ),
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

    # update consumed tokens
    tokens_this_run: int = result.get("tokens_used", 0)
    if tokens_this_run > 0:
        agent.tokens_used = agent.tokens_used + tokens_this_run
        await db.flush()

    # auto-send reply via channel if session has channel_type configured
    reply = result["reply"]
    if session.channel_type and reply:
        try:
            from app.core.channel_service import send_message as channel_send
            await channel_send(
                channel_type=session.channel_type,
                channel_config=session.channel_config or {},
                text=reply,
            )
        except Exception:
            pass  # channel send is best-effort

    return MessageResponse(
        reply=reply,
        steps=[StepSummary(**s) for s in result["steps"]],
        run_id=result["run_id"],
    )
