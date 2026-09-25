"""Durable outbound-message queue helpers.

The queue deliberately stores only the latest unsent message for one agent and
recipient.  A newer customer turn or owner-approved reply therefore replaces a
stale pending response instead of delivering it later out of context.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.engine.wa_outbound_guard import normalize_wa_outbound_target
from app.models.outbound_message import OutboundMessage


async def enqueue_outbound_message(
    db: AsyncSession,
    *,
    agent_id: Any,
    session_id: Any,
    target: str,
    text: str,
    source_device_id: str = "",
    kind: str = "reply_to_customer",
) -> OutboundMessage:
    """Queue the latest reply for a recipient, superseding unsent older text."""
    normalized_target = normalize_wa_outbound_target(target) or str(target).strip()
    now = datetime.now(timezone.utc)
    await db.execute(
        update(OutboundMessage)
        .where(
            OutboundMessage.agent_id == agent_id,
            OutboundMessage.target == normalized_target,
            OutboundMessage.status == "queued",
        )
        .values(status="cancelled", last_error="superseded_by_newer_message")
    )
    item = OutboundMessage(
        agent_id=agent_id,
        session_id=session_id,
        target=normalized_target,
        source_device_id=str(source_device_id or ""),
        text=text,
        kind=kind,
        status="queued",
        available_at=now,
    )
    db.add(item)
    await db.flush()
    return item


async def cancel_queued_outbound_for_target(
    db: AsyncSession,
    *,
    agent_id: Any,
    target: str | None,
    reason: str = "superseded_by_newer_customer_message",
) -> int:
    """Cancel stale pending replies when that recipient sends a newer turn."""
    normalized_target = normalize_wa_outbound_target(target)
    if not normalized_target:
        return 0
    result = await db.execute(
        update(OutboundMessage)
        .where(
            OutboundMessage.agent_id == agent_id,
            OutboundMessage.target == normalized_target,
            OutboundMessage.status == "queued",
        )
        .values(status="cancelled", last_error=reason)
    )
    return int(result.rowcount or 0)
