import asyncio
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.engine.agent_runner import run_agent
from app.core.engine.arthur_admission import ArthurQueueFull, arthur_run_slot
from app.core.engine.session_lock import (
    cancel_active_run,
    is_latest_session_turn,
    mark_latest_session_turn,
    register_active_task,
    session_run_lock,
    unregister_active_task,
)
from app.core.domain.agent_quota_service import check_agent_quota, record_agent_token_usage
from app.database import get_db
from app.models.agent import Agent
from app.models.message import Message
from app.models.session import Session
from app.core.utils.wa_identity import resolve_auto_provision_external_id
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

    quota_check = await check_agent_quota(agent, db)
    if not quota_check.allowed:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=quota_check.detail,
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

    # Auto-provision user ke DB saat pertama kali chat Arthur (builder agent).
    # Untuk WA, hanya pakai phone_number yang sudah ter-resolve; jangan simpan LID/JID.
    _ext_uid = resolve_auto_provision_external_id(
        channel_type=session.channel_type,
        channel_config=session.channel_config if isinstance(session.channel_config, dict) else {},
        payload_external_user_id=payload.external_user_id,
        session_external_user_id=session.external_user_id,
    )
    if _ext_uid and agent.tools_config and agent.tools_config.get("builder"):
        try:
            from app.core.domain.subscription_service import get_or_create_wa_user
            await get_or_create_wa_user(_ext_uid, db)
            await db.commit()  # commit sekarang agar tidak ikut rollback kalau run_agent gagal
        except Exception:
            pass  # provisioning gagal tidak boleh block chat

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

    # Mark this inbound message as the latest turn before cancelling/waiting.
    # Spam bursts can otherwise queue many HTTP handlers behind the session lock
    # and replay stale prompts/replies one-by-one after the active run ends.
    turn_generation = await mark_latest_session_turn(session_id)

    # Cancel any in-progress run for this session (human interrupt).
    # This handles the case where user sends a new message while the agent
    # is still working (e.g. subagent taking too long).
    _prior_interrupted = await cancel_active_run(session_id)

    await db.commit()
    try:
        async with session_run_lock(session_id):
            if not await is_latest_session_turn(session_id, turn_generation):
                return MessageResponse(reply="", steps=[], run_id=None)
            current_task = asyncio.current_task()
            if current_task:
                await register_active_task(session_id, current_task)
            async with arthur_run_slot(agent, payload.message):
                result = await run_agent(
                    agent_model=agent,
                    session=session,
                    user_message=payload.message,
                    db=db,
                    prior_run_was_interrupted=_prior_interrupted,
                )
            if not await is_latest_session_turn(session_id, turn_generation):
                return MessageResponse(reply="", steps=[], run_id=result.get("run_id"))
    except asyncio.CancelledError:
        # This run was interrupted by a subsequent message from the same user.
        # The new request will handle the reply — nothing to return here.
        from app.core.engine.agent_runner import persist_cancelled_run_for_task

        await asyncio.shield(persist_cancelled_run_for_task(asyncio.current_task()))
        raise
    except ArthurQueueFull as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arthur sedang menerima terlalu banyak permintaan. Silakan coba lagi sebentar lagi.",
        ) from exc
    finally:
        await unregister_active_task(session_id, asyncio.current_task())

    # update consumed tokens
    tokens_this_run: int = result.get("tokens_used", 0)
    if tokens_this_run > 0:
        await record_agent_token_usage(agent, tokens_this_run, db)
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
