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
"""
from __future__ import annotations

import re as _re
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
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


class WAIncomingMessage(BaseModel):
    device_id: str
    from_: str = Field(..., alias="from")
    chat_id: str | None = None  # group JID (xxx@g.us) atau nomor DM; kalau None fallback ke from_
    message: str
    timestamp: int | None = None
    # Media fields — diisi oleh Go service saat pesan mengandung gambar/dokumen/sticker
    media_type: str | None = None      # "image" | "document" | "sticker" | None
    media_data: str | None = None      # base64-encoded raw bytes
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
    is_operator = bool(operator_phone and from_phone and from_phone == operator_phone)

    if is_operator:
        # Pesan dari operator → inject sebagai perintah operator ke agent
        user_message = f"[OPERATOR] {raw_message}"
        log.info("channels.incoming.operator_command")
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
                    channel_config=session.channel_config if isinstance(session.channel_config, dict) else {},
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


@router.post("/wa/incoming")
async def wa_incoming(
    body: WAIncomingMessage,
    db: AsyncSession = Depends(get_db),
):
    """
    Webhook called by the Go wa-service when a WhatsApp message arrives.
    Finds the agent by device_id, finds or creates a session for the sender,
    runs the agent, and replies via wa-service.
    """
    log = logger.bind(device_id=body.device_id, from_phone=body.from_)

    # Find agent by wa_device_id
    agent_result = await db.execute(
        select(Agent).where(
            Agent.wa_device_id == body.device_id,
            Agent.is_deleted.is_(False),
        )
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        log.warning("wa_incoming.agent_not_found")
        raise HTTPException(status_code=404, detail="No agent found for this WhatsApp device")

    from_phone = body.from_
    # chat_id: target untuk mengirim reply (grup JID atau nomor DM)
    reply_target = body.chat_id or body.from_
    raw_message = body.message
    escalation_cfg: dict = agent.escalation_config or {}
    operator_phone: str = escalation_cfg.get("operator_phone", "")

    # Normalisasi: strip "+" prefix dan "@domain" suffix (WA JID: 62xxx@s.whatsapp.net, @g.us, @lid, dll)
    def _normalize_phone(p: str) -> str:
        return p.lstrip("+").split("@")[0]

    # Pesan dianggap dari operator jika from_ ATAU chat_id cocok dengan operator_phone.
    # Fallback ke chat_id diperlukan karena Go WA service kadang mengisi from_ dengan
    # JID pengirim asli pesan yang di-quote (user), bukan nomor operator yang membalas.
    is_operator = bool(
        operator_phone and (
            (_normalize_phone(from_phone) == _normalize_phone(operator_phone))
            or (reply_target and _normalize_phone(reply_target) == _normalize_phone(operator_phone))
        )
    )

    # Cari user JID yang sedang dalam eskalasi (untuk context operator)
    escalation_user_jid: str | None = None
    escalation_context: str | None = None
    if is_operator:
        from app.models.message import Message as _Msg
        from sqlalchemy import desc as _desc
        
        esc_result = await db.execute(
            select(Session)
            .join(_Msg, _Msg.session_id == Session.id)
            .where(
                Session.agent_id == agent.id,
                _Msg.role == "escalation"
            )
            .order_by(_desc(_Msg.timestamp))
            .limit(1)
        )
        esc_session = esc_result.scalars().first()
        if esc_session:
            _raw_ch = esc_session.channel_config
            ch = _raw_ch if isinstance(_raw_ch, dict) else {}
            escalation_user_jid = ch.get("user_phone") or esc_session.external_user_id

            # Ambil pesan user terkini dari sesi eskalasi agar agent punya konteks
            from app.models.message import Message as _Msg
            from sqlalchemy import desc as _desc
            _recent = await db.execute(
                select(_Msg)
                .where(
                    _Msg.session_id == esc_session.id,
                    _Msg.role == "user",
                )
                .order_by(_desc(_Msg.step_index))
                .limit(5)
            )
            recent_msgs = list(reversed(_recent.scalars().all()))
            if recent_msgs:
                lines = []
                for m in recent_msgs:
                    content = m.content or ""
                    # Strip internal prefix agar operator lihat pesan asli user
                    content = content.removeprefix("[USER_IN_ESCALATION] ").strip()
                    if content:
                        lines.append(f"- {content}")
                if lines:
                    escalation_context = "\n".join(lines)

    # Tentukan external_user_id untuk session lookup:
    # - operator → pakai operator_phone (session milik operator sendiri)
    # - pesan grup → pakai group JID (chat_id berakhiran @g.us) agar semua member berbagi satu session grup
    # - DM → pakai nomor pengirim (body.from_)
    is_group = bool(body.chat_id and body.chat_id.endswith("@g.us"))
    if is_operator:
        lookup_user_id = operator_phone
    elif is_group:
        lookup_user_id = body.chat_id
    else:
        lookup_user_id = body.from_

    session = None
    # Cari session berdasarkan agent_id + external_user_id
    # Operator → lookup by operator_phone (session milik operator sendiri)
    # User biasa → lookup by body.from_
    session_result = await db.execute(
        select(Session).where(
            Session.agent_id == agent.id,
            Session.channel_type == "whatsapp",
            Session.external_user_id == lookup_user_id,
        )
    )
    session = session_result.scalars().first()

    # effective_reply_target: JID tujuan saat membalas.
    # - Grup: wajib pakai chat_id (@g.us JID)
    # - Operator DM: pakai reply_target (nomor operator)
    # - User DM biasa: pakai body.from_ (nomor pengirim, bukan chat_id)
    #   karena chat_id bisa berupa @lid JID yang routing-nya tidak reliable;
    #   nomor telepon (@s.whatsapp.net) lebih stabil dan WhatsApp routing ke LID otomatis.
    if is_group or is_operator:
        effective_reply_target = reply_target
    else:
        effective_reply_target = body.from_

    if session:
        # Pastikan device_id dan user_phone (reply JID) selalu up-to-date
        _raw_cfg = session.channel_config
        new_config = dict(_raw_cfg) if isinstance(_raw_cfg, dict) else {}
        if new_config.get("device_id") != body.device_id or new_config.get("user_phone") != effective_reply_target:
            new_config["device_id"] = body.device_id
            new_config["user_phone"] = effective_reply_target
            session.channel_config = new_config
            await db.flush()

    if session is None:
        session = Session(
            agent_id=agent.id,
            external_user_id=lookup_user_id,
            channel_type="whatsapp",
            channel_config={
                "user_phone": effective_reply_target,
                "device_id": body.device_id,
            },
        )
        db.add(session)
        await db.flush()
        await db.refresh(session)
        log.info("wa_incoming.session_created", session_id=str(session.id), is_operator=is_operator)

    # --- Proses media (gambar/dokumen) jika ada ---
    media_context = ""
    media_image_b64: str | None = None
    media_image_mime: str | None = None

    if body.media_type and body.media_data:
        try:
            import base64 as _b64
            from app.core.sandbox import get_workspace_dir

            raw_bytes = _b64.b64decode(body.media_data)
            workspace = get_workspace_dir(session.id)
            filename = body.media_filename or f"incoming_{body.media_type}"
            if "." not in filename:
                ext_map = {"image": ".jpg", "document": ".bin", "sticker": ".webp"}
                filename += ext_map.get(body.media_type, ".bin")
            target_path = workspace / filename
            target_path.write_bytes(raw_bytes)
            log.info("wa_incoming.media_saved", media_type=body.media_type, filename=filename)

            if body.media_type == "image":
                # Kirim gambar sebagai konten multimodal ke LLM
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
                mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
                media_image_mime = mime_map.get(ext, "image/jpeg")
                media_image_b64 = body.media_data
                media_context = f"\n[Gambar diterima dan ditampilkan di atas. File juga tersimpan di /workspace/{filename}]"

            elif body.media_type == "document":
                # Ekstrak teks dari dokumen dan sertakan dalam pesan
                ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
                doc_extractable = {".pdf", ".docx", ".pptx", ".txt", ".md", ".csv"}
                if ext in doc_extractable:
                    try:
                        from app.core.file_processor import extract_text
                        from app.config import get_settings as _get_settings
                        extracted = await extract_text(
                            content=raw_bytes,
                            filename=filename,
                            content_type=None,
                            mistral_api_key=_get_settings().mistral_api_key,
                        )
                        # Batasi panjang teks agar tidak membanjiri konteks
                        MAX_CHARS = 12000
                        if len(extracted) > MAX_CHARS:
                            extracted = extracted[:MAX_CHARS] + f"\n... [dipotong, total {len(extracted)} karakter]"
                        media_context = (
                            f"\n[Dokumen diterima: {filename}]\n"
                            f"Isi dokumen:\n```\n{extracted}\n```"
                        )
                    except Exception as exc:
                        log.warning("wa_incoming.doc_extract_failed", error=str(exc))
                        media_context = f"\n[Dokumen diterima: {filename}, tersimpan di /workspace/{filename}]"
                else:
                    media_context = f"\n[Dokumen diterima: {filename}, tersimpan di /workspace/{filename}]"

            elif body.media_type == "sticker":
                media_context = f"\n[Stiker diterima, tersimpan di /workspace/{filename}]"

        except Exception as exc:
            log.warning("wa_incoming.media_save_failed", error=str(exc))

    if is_operator:
        user_message = raw_message + media_context
        log.info("wa_incoming.operator_session", escalation_user_jid=escalation_user_jid)
    else:
        user_message = raw_message + media_context
        log.info("wa_incoming.normal")

    # Run agent
    from app.core.agent_runner import run_agent

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
        )
    except Exception as exc:
        log.error("wa_incoming.agent_error", error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent error: {exc}")

    reply = result.get("reply", "")

    steps = result.get("steps", [])
    messages_to_user = []
    for step in steps:
        if step.get("tool") == "reply_to_user":
            msg = step.get("args", {}).get("message") or ""
            if not msg:
                res_str = step.get("result", "")
                m = _re.search(r'\[SENT_TO_USER\]\s*(.+)', res_str, _re.DOTALL)
                if m:
                    msg = m.group(1).strip()
            if msg:
                messages_to_user.append({"type": "reply_to_user", "message": msg})
        elif step.get("tool") == "send_to_number":
            msg = step.get("args", {}).get("message") or ""
            target = step.get("args", {}).get("phone_or_target") or ""
            if msg:
                messages_to_user.append({"type": "send_to_number", "message": msg, "target": target})

    # --- Kirim reply ke channel ---
    if reply:
        try:
            from app.core.text_utils import markdown_to_wa
            from app.core.wa_client import send_wa_message
            wa_reply = markdown_to_wa(reply)
            if is_operator:
                # Kirim final reply ke operator (session milik operator sendiri)
                # Jika agent juga memanggil reply_to_user, pesan itu sudah dikirim oleh tool
                await send_wa_message(body.device_id, reply_target, wa_reply)
            else:
                # Balas ke user; guard: jangan kirim ke nomor operator
                normalized_target = reply_target.lstrip("+").split("@")[0]
                normalized_operator = operator_phone.lstrip("+").split("@")[0] if operator_phone else ""
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
        "messages_to_user": messages_to_user,
    }
