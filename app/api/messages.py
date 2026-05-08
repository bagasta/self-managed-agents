import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.engine.agent_runner import run_agent
from app.core.engine.session_lock import (
    cancel_active_run,
    register_active_task,
    session_run_lock,
    unregister_active_task,
)
from app.database import get_db
from app.models.agent import Agent
from app.models.message import Message
from app.models.session import Session
from app.schemas.message import MessageCreate, MessageResponse, StepSummary

router = APIRouter(prefix="/v1/agents", tags=["messages"])
limiter = Limiter(key_func=get_remote_address)


@router.post(
    "/{agent_id}/sessions/{session_id}/messages",
    response_model=MessageResponse,
)
@limiter.limit("20/minute")
async def send_message(
    request: Request,
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

    # /reset — intercept sebelum agent run
    if payload.message.strip().lower() == "/reset":
        await db.execute(delete(Message).where(Message.session_id == session_id))
        session.metadata_ = {}
        db.add(session)
        await db.commit()
        from app.core.engine import interrupt_store as _istore
        await _istore.clear_interrupt(session_id)
        return MessageResponse(
            reply="Percakapan direset. Memori sesi ini telah dibersihkan.",
            steps=[],
            run_id=None,
        )

    # Cancel any in-progress run for this session (human interrupt).
    # This handles the case where user sends a new message while the agent
    # is still working (e.g. subagent taking too long).
    _prior_interrupted = await cancel_active_run(session_id)

    current_task = asyncio.current_task()
    if current_task:
        await register_active_task(session_id, current_task)

    try:
        async with session_run_lock(session_id):
            result = await run_agent(
                agent_model=agent,
                session=session,
                user_message=payload.message,
                db=db,
                prior_run_was_interrupted=_prior_interrupted,
            )
    except asyncio.CancelledError:
        # This run was interrupted by a subsequent message from the same user.
        # The new request will handle the reply — nothing to return here.
        raise
    finally:
        await unregister_active_task(session_id)

    # update consumed tokens
    tokens_this_run: int = result.get("tokens_used", 0)
    if tokens_this_run > 0:
        agent.tokens_used = agent.tokens_used + tokens_this_run
        await db.flush()

    # auto-send reply via channel if session has channel_type configured
    reply = result["reply"]
    if session.channel_type and reply:
        try:
            from app.core.infra.channel_service import send_message as channel_send
            await channel_send(
                channel_type=session.channel_type,
                channel_config=session.channel_config if isinstance(session.channel_config, dict) else {},
                text=reply,
            )
        except Exception:
            pass  # channel send is best-effort

    return MessageResponse(
        reply=reply,
        steps=[StepSummary(**s) for s in result["steps"]],
        run_id=result["run_id"],
    )
