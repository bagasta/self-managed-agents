"""
Incoming channel webhook — endpoint untuk menerima pesan masuk dari channel eksternal
(WhatsApp webhook, Telegram webhook, dll).

Endpoint:
  POST /v1/channels/incoming/{session_id}

Agent membedakan pengirim:
  - Jika from_phone == agent.escalation_config.operator_phone
    → pesan dianggap PERINTAH dari operator
    → inject sebagai "[OPERATOR] {message}" ke agent
  - Jika escalation_active == True dan bukan dari operator
    → forward pesan user ke operator (via channel_service)
    → tetap jalankan agent dengan konteks eskalasi
  - Selain itu → proses normal
"""
from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_service import send_message
from app.database import get_db
from app.models.agent import Agent
from app.models.session import Session

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/channels", tags=["channels"])


class IncomingMessage(BaseModel):
    from_phone: str | None = None
    message: str


@router.post("/incoming/{session_id}")
async def incoming_message(
    session_id: uuid.UUID,
    body: IncomingMessage,
    db: AsyncSession = Depends(get_db),
):
    """
    Terima pesan masuk dari channel eksternal.
    Dipakai sebagai webhook target untuk WhatsApp, Telegram, dll.
    """
    # Load session
    sess_result = await db.execute(select(Session).where(Session.id == session_id))
    session = sess_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session tidak ditemukan")

    # Load agent
    agent_result = await db.execute(select(Agent).where(Agent.id == session.agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent tidak ditemukan")

    from_phone = body.from_phone or ""
    raw_message = body.message
    escalation_cfg: dict = agent.escalation_config or {}
    operator_phone: str = escalation_cfg.get("operator_phone", "")

    log = logger.bind(session_id=str(session_id), from_phone=from_phone)

    # --- Tentukan jenis pengirim ---
    is_operator = bool(operator_phone and from_phone and from_phone == operator_phone)

    if is_operator:
        # Pesan dari operator → inject sebagai perintah operator ke agent
        user_message = f"[OPERATOR] {raw_message}"
        log.info("channels.incoming.operator_command")
    elif session.escalation_active:
        # User biasa tapi eskalasi aktif → forward ke operator dulu, baru proses agent
        log.info("channels.incoming.user_escalation_active")
        channel_cfg = session.channel_config or {}
        user_phone = channel_cfg.get("user_phone", from_phone or str(session_id))
        forward_text = f"[USER {user_phone}]: {raw_message}"

        # Forward ke operator
        try:
            op_channel_cfg = {**escalation_cfg, "user_phone": operator_phone}
            await send_message(
                channel_type=escalation_cfg.get("channel_type", session.channel_type or ""),
                channel_config=op_channel_cfg,
                text=forward_text,
            )
        except Exception as exc:
            log.warning("channels.incoming.forward_failed", error=str(exc))

        user_message = f"[USER_IN_ESCALATION] {raw_message}"
    else:
        # Pesan normal dari user
        user_message = raw_message
        log.info("channels.incoming.normal")

    # --- Jalankan agent ---
    from app.core.agent_runner import run_agent

    try:
        result = await run_agent(
            agent_model=agent,
            session=session,
            user_message=user_message,
            db=db,
        )
    except Exception as exc:
        log.error("channels.incoming.agent_error", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Agent error: {exc}")

    reply = result.get("reply", "")

    # --- Kirim reply ke channel ---
    # Untuk perintah operator: reply dikirim ke operator (bukan ke user)
    # Untuk pesan user: reply dikirim ke user
    if session.channel_type and reply:
        try:
            if is_operator:
                # Balas ke operator
                op_cfg = {**escalation_cfg, "user_phone": operator_phone}
                await send_message(
                    channel_type=escalation_cfg.get("channel_type", session.channel_type),
                    channel_config=op_cfg,
                    text=reply,
                )
            else:
                # Balas ke user
                await send_message(
                    channel_type=session.channel_type,
                    channel_config=session.channel_config or {},
                    text=reply,
                )
        except Exception as exc:
            log.warning("channels.incoming.send_reply_failed", error=str(exc))

    # Ekstrak pesan yang dikirim ke user dari tool calls
    steps = result.get("steps", [])
    messages_to_user = []
    for step in steps:
        if step.get("tool") == "reply_to_user":
            msg = step.get("args", {}).get("message") or ""
            if not msg:
                # fallback: parse dari result string
                res_str = step.get("result", "")
                import re
                m = re.search(r'\[SENT_TO_USER\]\s*(.+)', res_str, re.DOTALL)
                if m:
                    msg = m.group(1).strip()
            if msg:
                messages_to_user.append({"type": "reply_to_user", "message": msg})
        elif step.get("tool") == "send_to_number":
            msg = step.get("args", {}).get("message") or ""
            target = step.get("args", {}).get("phone_or_target") or ""
            if msg:
                messages_to_user.append({"type": "send_to_number", "message": msg, "target": target})

    return {
        "status": "ok",
        "reply": reply,
        "run_id": str(result.get("run_id", "")),
        "steps": steps,
        "messages_to_user": messages_to_user,
    }
