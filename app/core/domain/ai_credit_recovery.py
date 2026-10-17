"""Recovery is limited to provider credit failures before any tool ran."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import aliased

from app.models.message import Message
from app.models.pending_ai_reply import PendingAIReply
from app.models.run import Run
from app.models.session import Session

RECOVERY_TTL = timedelta(hours=24)


def is_credit_exhaustion_error(error: object) -> bool:
    """Do not confuse Google storage quotas, rate limits, or auth with credits."""
    text = str(error).lower()
    return any(marker in text for marker in (
        "insufficient_quota", "insufficient credits", "insufficient_credits",
        "requires more credits", "not enough credits", "credit balance is too low",
        "openrouter_credits",
    )) or ("402" in text and any(word in text for word in ("credit", "payment required")))


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def enqueue_credit_recovery(db, *, run: Run, payload: dict | None = None):
    existing = await db.get(PendingAIReply, run.id)
    if existing is not None:
        return existing
    if run.status != "failed" or run.tokens_used or not is_credit_exhaustion_error(run.error_message):
        return None
    session = await db.get(Session, run.session_id)
    if session is None or session.channel_type != "whatsapp" or session.ai_disabled:
        return None
    message = (await db.execute(select(Message).where(
        Message.run_id == run.id, Message.role == "user",
    ).order_by(Message.timestamp.desc()).limit(1))).scalar_one_or_none()
    if message is None:
        return None
    has_output = (await db.execute(select(Message.id).where(
        Message.run_id == run.id, Message.role.in_(["agent", "tool"]),
    ).limit(1))).scalar_one_or_none()
    if has_output is not None:
        return None
    now = datetime.now(timezone.utc)
    item = PendingAIReply(
        id=run.id, session_id=session.id, message_id=message.id,
        status="waiting", attempts=0, available_at=now + timedelta(seconds=60),
        expires_at=utc(message.timestamp) + RECOVERY_TTL, payload=payload or {},
    )
    db.add(item)
    await db.flush()
    return item


async def is_current_request(db, message: Message, *, ignore_run_id=None) -> bool:
    """A newer customer turn or answer makes this old request obsolete."""
    session = await db.get(Session, message.session_id)
    if session is None:
        return False
    peers = select(Session.id).where(Session.id == session.id)
    if session.external_user_id:
        peers = select(Session.id).where(
            Session.agent_id == session.agent_id,
            Session.external_user_id == session.external_user_id,
            Session.channel_type == session.channel_type,
        )
    query = select(Message.id).where(
        Message.session_id.in_(peers),
        or_(
            Message.timestamp > message.timestamp,
            (Message.timestamp == message.timestamp) & (Message.step_index > message.step_index),
        ),
        Message.role.in_(["user", "agent"]),
        Message.id != message.id,
    )
    if ignore_run_id is not None:
        query = query.where(or_(Message.run_id.is_(None), Message.run_id != ignore_run_id))
    newer = (await db.execute(query.limit(1))).scalar_one_or_none()
    return newer is None


async def backfill_credit_recovery(db, *, agent_id, since: datetime, apply: bool = False) -> dict:
    """Explicit, repeatable recovery of historical failures; dry-run by default."""
    newer = aliased(Message)
    candidates = (await db.execute(select(Run, Message).join(
        Session, Session.id == Run.session_id,
    ).join(Message, Message.run_id == Run.id).where(
        Session.agent_id == agent_id, Session.channel_type == "whatsapp",
        Session.ai_disabled.is_(False), Run.status == "failed", Run.tokens_used == 0,
        Run.created_at >= since, Message.role == "user",
        ~exists(select(PendingAIReply.id).where(PendingAIReply.id == Run.id)),
        ~exists(select(newer.id).where(
            newer.session_id == Message.session_id,
            or_(
                newer.timestamp > Message.timestamp,
                (newer.timestamp == Message.timestamp) & (newer.step_index > Message.step_index),
            ),
            newer.role.in_(["user", "agent"]),
        )),
    ))).all()
    result = {"eligible": 0, "enqueued": 0, "expired": 0}
    now = datetime.now(timezone.utc)
    for run, message in candidates:
        if not is_credit_exhaustion_error(run.error_message):
            continue
        if not await is_current_request(db, message):
            continue
        if utc(message.timestamp) + RECOVERY_TTL <= now:
            result["expired"] += 1
            continue
        result["eligible"] += 1
        if apply and await enqueue_credit_recovery(db, run=run) is not None:
            result["enqueued"] += 1
    if apply:
        await db.commit()
    return result
