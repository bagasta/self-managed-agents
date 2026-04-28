"""
Incoming channel webhook — endpoint untuk menerima pesan masuk dari channel eksternal
(WhatsApp webhook, Telegram webhook, dll).

Endpoint:
  POST /v1/channels/incoming/{session_id}   — generic channel webhook
  POST /v1/channels/wa/incoming             — WhatsApp-specific webhook dari Go wa-service

Desain operator session (WhatsApp):
  - Setiap pengirim (termasuk operator) memiliki session SENDIRI berdasarkan nomor pengirim.
  - Jika from_phone == agent.escalation_config.operator_phone:
      → Operator punya session operator (lookup by operator_phone).
      → agent_runner dijalankan dengan escalation_user_jid = JID user yang sedang dieskalasi
        (diambil dari session dengan escalation_active=True).
      → Sistem prompt memberi tahu agent bahwa ia sedang di sesi OPERATOR dan harus
        menggunakan tool reply_to_user(...) untuk mengirim ke user.
  - Jika escalation_active == True dan bukan dari operator:
      → Forward pesan user ke operator (via channel_service) sebagai notifikasi.
      → Jalankan agent dengan pesan [USER_IN_ESCALATION] untuk konteks.
  - Selain itu → proses normal per-user.

Single-worker constraint:
  Event bus (SSE) masih in-memory dengan Redis fallback (jika REDIS_URL di-set).
  Jangan jalankan lebih dari 1 uvicorn worker sampai Redis terkonfigurasi penuh.
  Lihat docs/production-plan/01-critical-blockers.md#1.2 untuk detail.
"""
from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.channel_service import send_message
from app.core.input_sanitizer import sanitize_user_input
from app.core.phone_utils import normalize_phone
from app.core.text_utils import markdown_to_wa
from app.core.wa_client import send_wa_message
from app.database import get_db
from app.models.agent import Agent
from app.models.session import Session

# Helper functions untuk wa_incoming — dipecah agar bisa di-test secara independen
from app.api.wa_helpers import (
    extract_messages_to_user,
    find_agent_by_device,
    find_escalation_context,
    find_or_create_wa_session,
    get_wa_lookup_user_id,
    is_operator_message,
    process_wa_media,
    is_duplicate_message,
)

_settings = get_settings()
_DEVELOPER_PHONE: str = _settings.developer_phone
_GENERIC_ERROR_MSG = "Maaf, terjadi gangguan sementara. Silakan coba lagi dalam beberapa saat."

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/channels", tags=["channels"])


@router.get("/wa-dev/operator-route")
async def wa_dev_operator_route(
    phone: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Used by wa-dev-service: given a phone number, find which agent this phone is an operator for.
    Returns {agent_id, agent_name} or 404 if not an operator for any agent.
    This allows wa-dev to auto-route operator messages without requiring the operator
    to do 'connect {agentID}' explicitly.
    """
    normalized = normalize_phone(phone)
    agents_result = await db.execute(
        select(Agent).where(Agent.is_deleted.is_(False))
    )
    for agent in agents_result.scalars().all():
        op_ids: list = getattr(agent, "operator_ids", None) or []
        esc_cfg: dict = agent.escalation_config or {}
        op_phone: str = esc_cfg.get("operator_phone", "")

        all_ops = {normalize_phone(p) for p in op_ids if p}
        if op_phone:
            all_ops.add(normalize_phone(op_phone))

        if normalized in all_ops:
            return {"agent_id": str(agent.id), "agent_name": agent.name}

    raise HTTPException(status_code=404, detail="Not an operator for any agent")


class IncomingMessage(BaseModel):
    from_phone: str | None = None
    message: str = Field(..., max_length=10_000)


class WAIncomingMessage(BaseModel):
    device_id: str
    from_: str = Field(..., alias="from")
    phone_from: str | None = None      # resolved phone number from Go (LID → phone); fallback ke from_
    chat_id: str | None = None  # group JID (xxx@g.us) atau nomor DM; kalau None fallback ke from_
    message: str = Field(..., max_length=10_000)
    timestamp: int | None = None
    push_name: str | None = None       # WhatsApp display name of sender
    # Media fields — diisi oleh Go service saat pesan mengandung gambar/dokumen/sticker
    media_type: str | None = None      # "image" | "document" | "sticker" | None
    media_data: str | None = Field(None, max_length=10_000_000)  # base64-encoded raw bytes
    media_filename: str | None = None  # original filename (dokumen) atau generated (gambar)

    model_config = {"populate_by_name": True}


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
    _op_ids: list = getattr(agent, "operator_ids", None) or []
    _norm_op_ids = {normalize_phone(oid) for oid in _op_ids if oid}
    if operator_phone:
        _norm_op_ids.add(normalize_phone(operator_phone))
    is_operator = bool(_norm_op_ids and from_phone and normalize_phone(from_phone) in _norm_op_ids)

    if is_operator:
        user_message = f"[OPERATOR] {raw_message}"
        log.info("channels.incoming.operator_command")
    else:
        user_message = raw_message
        log.info("channels.incoming.normal")

    # --- Jalankan agent ---
    from app.core.agent_runner import run_agent  # deferred to avoid circular import

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
    if session.channel_type and reply:
        try:
            if is_operator:
                op_cfg = {**escalation_cfg, "user_phone": operator_phone}
                await send_message(
                    channel_type=escalation_cfg.get("channel_type", session.channel_type),
                    channel_config=op_cfg,
                    text=reply,
                )
            else:
                await send_message(
                    channel_type=session.channel_type,
                    channel_config=session.channel_config if isinstance(session.channel_config, dict) else {},
                    text=reply,
                )
        except Exception as exc:
            log.warning("channels.incoming.send_reply_failed", error=str(exc))

    steps = result.get("steps", [])
    return {
        "status": "ok",
        "reply": reply,
        "run_id": str(result.get("run_id", "")),
        "steps": steps,
        "messages_to_user": extract_messages_to_user(steps),
    }


@router.post("/wa/incoming")
async def wa_incoming(
    body: WAIncomingMessage,
    db: AsyncSession = Depends(get_db),
):
    """
    Webhook called by the Go wa-service when a WhatsApp message arrives.
    Finds the agent by device_id, finds or creates a session for the sender,
    runs the agent, and replies via wa-service.

    Flow:
    1. Find agent by device_id (via find_agent_by_device)
    2. Check if sender is operator (via is_operator_message)
    3. If operator: load escalation context (via find_escalation_context)
    4. Find or create WA session (via find_or_create_wa_session)
    5. Process media if any (via process_wa_media)
    6. Run agent
    7. Send reply via wa-service
    """
    log = logger.bind(device_id=body.device_id, from_phone=body.from_)

    # 1. Find agent
    agent = await find_agent_by_device(body.device_id, db)
    if not agent:
        log.warning("wa_incoming.agent_not_found")
        raise HTTPException(status_code=404, detail="No agent found for this WhatsApp device")

    # phone_from: phone number yang sudah di-resolve dari LID oleh Go wa-service.
    # Untuk akun LID: body.from_ berisi LID number, body.phone_from berisi phone number asli.
    # Gunakan phone_from sebagai identifier utama untuk allowlist & operator check.
    from_phone = body.phone_from or body.from_
    reply_target = body.chat_id or body.from_

    # 1.5. Cek deduplikasi WA (handling multiple webhook calls for the same message)
    if body.timestamp:
        if await is_duplicate_message(body.device_id, from_phone, body.timestamp, db):
            log.info("wa_incoming.duplicate_ignored")
            return {"status": "ignored", "reason": "duplicate message"}

    # 2. Cek apakah pesan dari operator
    _is_operator = is_operator_message(from_phone, reply_target, agent)

    # 2.5. Fitur 1 — cek allowlist (hanya untuk non-operator)
    if not _is_operator:
        allowed = getattr(agent, "allowed_senders", None)
        if allowed:  # null/[] = semua diizinkan
            allowed_set = {normalize_phone(p) for p in allowed if p}
            # Cek terhadap from_phone DAN chat_id/reply_target
            # Untuk akun LID: Sender.User bisa berisi LID (bukan phone number).
            # chat_id (dari evt.Info.Chat.String()) mengandung format asli WA.
            # Salah satu dari keduanya pasti cocok dengan nomor yang user daftarkan.
            candidates = {normalize_phone(from_phone)}
            if reply_target:
                candidates.add(normalize_phone(reply_target))
            if not candidates.intersection(allowed_set):
                log.info("wa_incoming.blocked_sender", from_phone=from_phone, chat_id=reply_target)
                return {"status": "ignored", "reason": "sender not in allowlist"}

    # 3. Jika operator, cari context eskalasi
    escalation_user_jid: str | None = None
    escalation_context: str | None = None
    if _is_operator:
        escalation_user_jid, escalation_context = await find_escalation_context(agent, db)
        log.info("wa_incoming.operator_session", escalation_user_jid=escalation_user_jid)

    # Tentukan lookup_user_id untuk session
    lookup_user_id = get_wa_lookup_user_id(
        from_phone=from_phone,
        chat_id=body.chat_id,
        is_operator=_is_operator,
        agent=agent,
    )

    # effective_reply_target: selalu pakai chat_id (atau from_ fallback).
    # chat_id dari wa-service sudah mengandung server info yang benar (@s.whatsapp.net atau @lid).
    effective_reply_target = reply_target

    # 4. Find or create session
    session, was_created = await find_or_create_wa_session(
        agent=agent,
        lookup_user_id=lookup_user_id,
        effective_reply_target=effective_reply_target,
        device_id=body.device_id,
        db=db,
        is_operator=_is_operator,
    )
    if was_created:
        log.info("wa_incoming.session_created", session_id=str(session.id), is_operator=_is_operator)
        # Commit agar session_id visible ke koneksi DB terpisah (e.g. scheduler_tool)
        await db.commit()

    # 4.5. Fitur 2 — cek ai_disabled (hanya untuk non-operator)
    if not _is_operator and getattr(session, "ai_disabled", False):
        log.info("wa_incoming.ai_disabled", session_id=str(session.id))
        return {"status": "ai_disabled"}

    # 5. Proses media jika ada
    media_context = ""
    media_image_b64: str | None = None
    media_image_mime: str | None = None

    if body.media_type and body.media_data:
        media_context, media_image_b64, media_image_mime = await process_wa_media(
            media_type=body.media_type,
            media_data=body.media_data,
            media_filename=body.media_filename,
            session_id=session.id,
            logger=log,
        )

    user_message = sanitize_user_input(body.message) + media_context
    if not _is_operator:
        log.info("wa_incoming.normal")

    sender_name: str | None = body.push_name or None

    # 6. Run agent
    from app.core.agent_runner import run_agent  # deferred to avoid circular import

    try:
        result = await run_agent(
            agent_model=agent,
            session=session,
            user_message=user_message,
            db=db,
            escalation_user_jid=escalation_user_jid,
            escalation_context=escalation_context,
            media_image_b64=media_image_b64,
            media_image_mime=media_image_mime,
            sender_name=sender_name,
        )
    except Exception as exc:
        log.error("wa_incoming.agent_error", error=str(exc), exc_info=True)
        import traceback as _tb
        err_detail = _tb.format_exc()
        if _DEVELOPER_PHONE:
            try:
                await send_wa_message(
                    body.device_id,
                    _DEVELOPER_PHONE,
                    f"⚠️ *Agent Error*\nAgent: {agent.name}\nFrom: {from_phone}\n\n```\n{err_detail[:3000]}\n```",
                )
            except Exception as _notify_exc:
                log.warning("wa_incoming.developer_notify_failed", error=str(_notify_exc))
        try:
            await send_wa_message(body.device_id, effective_reply_target, _GENERIC_ERROR_MSG)
        except Exception as _send_exc:
            log.warning("wa_incoming.error_reply_failed", error=str(_send_exc))
        return {"status": "error", "reply": _GENERIC_ERROR_MSG, "run_id": "", "steps": [], "messages_to_user": []}

    reply = result.get("reply", "")
    steps = result.get("steps", [])

    # 7. Kirim reply ke channel
    if reply:
        try:
            wa_reply = markdown_to_wa(reply)
            escalation_cfg: dict = agent.escalation_config or {}
            operator_phone: str = escalation_cfg.get("operator_phone", "")

            if _is_operator:
                # Kirim final reply ke operator
                await send_wa_message(body.device_id, reply_target, wa_reply)
            else:
                # Guard: jangan kirim ke nomor operator
                normalized_target = normalize_phone(reply_target)
                normalized_operator = normalize_phone(operator_phone) if operator_phone else ""
                if normalized_operator and normalized_target == normalized_operator:
                    log.warning("wa_incoming.reply_target_is_operator_suppressed", reply_target=reply_target)
                else:
                    await send_wa_message(body.device_id, reply_target, wa_reply)
        except Exception as exc:
            log.error("wa_incoming.send_reply_failed", target=reply_target, error=str(exc))

    return {
        "status": "ok",
        "reply": reply,
        "run_id": str(result.get("run_id", "")),
        "steps": steps,
        "messages_to_user": extract_messages_to_user(steps),
    }
