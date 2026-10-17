"""Resume credit-blocked WhatsApp requests and hand replies to the send queue."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import or_, select

from app.core.domain.ai_credit_recovery import (
    is_credit_exhaustion_error,
    is_current_request,
    utc,
)
from app.database import AsyncSessionLocal
from app.models.agent import Agent
from app.models.message import Message
from app.models.pending_ai_reply import PendingAIReply
from app.models.run import Run
from app.models.session import Session

logger = structlog.get_logger(__name__)
_STALE_AFTER = timedelta(minutes=30)


async def claim_due_replies(limit: int = 5) -> list[uuid.UUID]:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        items = list((await db.execute(select(PendingAIReply).where(or_(
            (PendingAIReply.status == "waiting") & (PendingAIReply.available_at <= now),
            (PendingAIReply.status == "processing") & (PendingAIReply.claimed_at < now - _STALE_AFTER),
        )).order_by(PendingAIReply.available_at).limit(limit).with_for_update(skip_locked=True))).scalars())
        for item in items:
            item.status = "processing"
            item.claimed_at = now
        await db.commit()
        return [item.id for item in items]


def _finish(item, status: str, reason: str | None = None) -> None:
    item.status = status
    item.last_error = reason
    item.payload = {}


def _defer(item, error: str) -> None:
    item.status = "waiting"
    item.last_error = error[:2000]
    item.available_at = datetime.now(timezone.utc) + timedelta(
        seconds=min(900, 60 * 2 ** min(item.attempts, 4)),
    )
    item.retry_run_id = None


async def _execute_request(**kwargs):
    from app.core.engine.agent_runner import run_agent
    return await run_agent(**kwargs)


async def _allowed(db, item, session, agent, message) -> bool:
    from app.core.domain.agent_service_controls import service_block_reason
    if utc(item.expires_at) <= datetime.now(timezone.utc):
        _finish(item, "expired", "recovery_window_expired")
    elif not session or not agent or not message or agent.is_deleted:
        _finish(item, "cancelled", "session_agent_or_message_unavailable")
    elif session.channel_type != "whatsapp" or session.ai_disabled:
        _finish(item, "cancelled", "ai_disabled_or_channel_changed")
    elif service_block_reason(agent, session.external_user_id, whatsapp=True):
        _finish(item, "cancelled", "agent_service_blocked")
    elif not await is_current_request(db, message, ignore_run_id=item.retry_run_id):
        _finish(item, "cancelled", "superseded_by_newer_turn")
    else:
        return True
    return False


async def run_pending_reply(item_id: uuid.UUID) -> None:
    from app.core.domain.agent_quota_service import record_agent_token_usage
    from app.core.domain.outbound_queue_service import enqueue_outbound_message
    from app.core.engine.session_lock import session_run_lock
    from app.core.engine.wa_reply_delivery import should_skip_whatsapp_final_reply
    from app.core.workers.scheduler_service import _scheduled_channel_config

    async with AsyncSessionLocal() as db:
        item = await db.get(PendingAIReply, item_id)
        if item is None or item.status != "processing":
            return
        session = await db.get(Session, item.session_id)
        message = await db.get(Message, item.message_id)
        agent = await db.get(Agent, session.agent_id) if session else None
        if not await _allowed(db, item, session, agent, message):
            await db.commit()
            return
        if item.retry_run_id and await db.get(Run, item.retry_run_id) is not None:
            # The worker died after starting a model/tool run. Its outcome may
            # include external writes, so never repeat it blindly.
            _finish(item, "failed", "interrupted_retry_requires_review")
            await db.commit()
            return
        running = (await db.execute(select(Run.id).where(
            Run.session_id == session.id, Run.status == "running",
        ).limit(1))).scalar_one_or_none()
        if running is not None:
            _defer(item, "session_has_active_run")
            await db.commit()
            return
        item.retry_run_id = item.retry_run_id or uuid.uuid4()
        item.attempts += 1
        await db.commit()
        try:
            async with session_run_lock(session.id):
                result = await _execute_request(
                    agent_model=agent, session=session, user_message=message.content or "", db=db,
                    retry_run_id=item.retry_run_id, retry_message_id=message.id,
                    **dict(item.payload or {}),
                )
            run = await db.get(Run, item.retry_run_id)
            if run and run.status == "failed" and not run.tokens_used and is_credit_exhaustion_error(run.error_message):
                _defer(item, run.error_message or "insufficient_credits")
            elif not run or run.status != "completed":
                _finish(item, "failed", "retry_did_not_complete")
            else:
                tokens = int(result.get("tokens_used") or 0)
                if tokens:
                    await record_agent_token_usage(agent, tokens, db)
                await db.refresh(session)
                await db.refresh(agent)
                if await _allowed(db, item, session, agent, message):
                    reply = str(result.get("reply") or "").strip()
                    if not reply:
                        _finish(item, "failed", "retry_returned_empty_reply")
                    else:
                        # Transfer generation + delivery state atomically. A
                        # send failure retries text, never the model/tools.
                        message.run_id = run.id
                        if not should_skip_whatsapp_final_reply(reply, result.get("steps") or []):
                            cfg = _scheduled_channel_config(session, agent)
                            outbound = await enqueue_outbound_message(
                                db, agent_id=agent.id, session_id=session.id,
                                target=cfg.get("user_phone") or session.external_user_id,
                                source_device_id=cfg.get("device_id") or "",
                                text=reply, kind="ai_credit_recovery",
                            )
                            item.outbound_message_id = outbound.id
                        _finish(item, "completed")
                else:
                    run.status = "cancelled"
                    run.error_message = "Recovery superseded or service disabled before delivery."
            await db.commit()
            logger.info("ai_credit_recovery.result", item_id=str(item_id), status=item.status, attempts=item.attempts)
        except Exception as exc:  # noqa: BLE001 -- isolate model/tool failures per queue item
            await db.rollback()
            item = await db.get(PendingAIReply, item_id)
            run = await db.get(Run, item.retry_run_id) if item.retry_run_id else None
            if run and run.status == "failed" and not run.tokens_used and is_credit_exhaustion_error(run.error_message):
                _defer(item, run.error_message or str(exc))
            else:
                _finish(item, "failed", f"retry_requires_review: {type(exc).__name__}")
            await db.commit()
            logger.warning("ai_credit_recovery.error", item_id=str(item_id), error_type=type(exc).__name__)
