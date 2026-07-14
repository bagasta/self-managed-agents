"""
Escalation tools — agent bisa eskalasi ke human operator dan kirim pesan ke channel.

Tools yang di-expose ke agent:
  escalate_to_human(reason, summary)  — aktifkan mode eskalasi
  reply_to_user(message)              — kirim pesan ke user via channel sesi
  send_to_number(phone, message)      — kirim pesan ke nomor/target lain

Saat escalation_active=True:
  - Setiap pesan user akan di-forward ke operator oleh /channels/incoming endpoint
  - Agent tetap jalan dan bisa menerima perintah dari operator
  - Operator bisa memerintahkan agent via pesan (agent bedakan berdasarkan sender)
"""
from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.agent import Agent
from app.models.session import Session

logger = structlog.get_logger(__name__)


def _normalize_jid(jid: str | None) -> str:
    """Strip @lid / @s.whatsapp.net suffix and return clean number."""
    if not jid:
        return ""
    return jid.split("@")[0]


import re as _re

def _clean_jid_from_text(text: str) -> str:
    """Remove @lid and @s.whatsapp.net suffixes from any phone numbers in text."""
    return _re.sub(r'(\d+)@(?:lid|s\.whatsapp\.net|c\.us)', r'\1', text)


def _looks_like_media_delivery_text(message: str) -> bool:
    lowered = (message or "").lower()
    if not any(marker in lowered for marker in ("file", "dokumen", "pdf", "gambar", "foto", "attachment", "lampiran")):
        return False
    return any(
        marker in lowered
        for marker in (
            "sudah saya kirim",
            "sudah dikirim",
            "sudah terkirim",
            "berhasil saya kirim",
            "saya kirim sekarang",
            "saya kirim file",
            "saya kirim dokumen",
            "saya kirim pdf",
            "saya akan kirim",
            "saya kirimkan",
            "berikut saya kirim",
            "berikut saya kirimkan",
            "kirim file pdf",
            "kirim pdf",
            "akan segera mengirim",
            "langsung kirim file",
            "mengirimkan file",
            "mengirim file",
            "mengirim dokumen",
            "silakan cek filenya",
            "cek filenya di attachment",
            "cek attachment",
            "cek lampiran",
            "cek file terlampir",
            "file terlampir",
            "attachment berikut",
            "lampiran berikut",
            "mohon tunggu sebentar",
            "siap saya kirim",
            "siap dikirim",
        )
    )


def _is_current_turn_media(media_meta: dict, current_turn_media: object) -> bool:
    if not isinstance(current_turn_media, dict):
        return False
    media_path = str(media_meta.get("workspace_path") or "")
    current_path = str(current_turn_media.get("workspace_path") or "")
    if media_path and current_path and media_path != current_path:
        return False
    media_message_id = str(media_meta.get("source_message_id") or "")
    current_message_id = str(current_turn_media.get("source_message_id") or "")
    if media_message_id and current_message_id and media_message_id != current_message_id:
        return False
    return bool(media_path or media_message_id)


def build_escalation_tools(
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    db_factory: async_sessionmaker,
    user_jid: str | None = None,
    sender_name: str | None = None,
    user_message: str = "",
    allow_reply_to_user: bool = False,
) -> list:

    async def _persist_escalation_message_ids(message_ids: list[str]) -> None:
        if not message_ids:
            return
        async with db_factory() as persist_db:
            stored_session = await persist_db.get(Session, session_id)
            if not stored_session:
                return
            meta = dict(stored_session.metadata_ or {})
            existing_ids = meta.get("escalation_message_ids")
            if not isinstance(existing_ids, list):
                existing_ids = []
            for message_id in message_ids:
                if message_id and message_id not in existing_ids:
                    existing_ids.append(message_id)
            meta["escalation_message_ids"] = existing_ids
            if existing_ids:
                meta["escalation_message_id"] = existing_ids[0]
            stored_session.metadata_ = meta
            persist_db.add(stored_session)
            await persist_db.commit()

    @tool
    async def escalate_to_human(reason: str, summary: str = "") -> str:
        """
        Eskalasi percakapan ke human operator.
        Gunakan saat tidak bisa menangani permintaan user, butuh persetujuan manusia,
        atau situasi sensitif yang memerlukan intervensi manusia.

        Args:
            reason  : Alasan eskalasi singkat (contoh: "permintaan refund di luar policy")
            summary : Ringkasan konteks percakapan untuk operator (opsional tapi direkomendasikan)
        """
        from app.models.message import Message

        async with db_factory() as db:
            sess_result = await db.execute(select(Session).where(Session.id == session_id))
            session = sess_result.scalar_one_or_none()
            if not session:
                return "[error] Session tidak ditemukan."

            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                return "[error] Agent tidak ditemukan."

            _raw_esc = agent.escalation_config
            escalation_cfg: dict = _raw_esc if isinstance(_raw_esc, dict) else {}
            if not escalation_cfg:
                return "[error] Agent belum dikonfigurasi escalation_config. Tambahkan operator_phone dan channel_type."

            operator_channel = escalation_cfg.get("channel_type", "")
            operator_phone = escalation_cfg.get("operator_phone", "")
            operator_config = {
                **escalation_cfg,
                "user_phone": operator_phone,
                "device_id": (session.channel_config or {}).get("device_id", "") if isinstance(session.channel_config, dict) else "",
            }

            import time
            case_id = f"esc_{int(time.time())}_{str(session_id)[:6]}"

            _raw_cfg = session.channel_config
            channel_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
            # phone_number = real phone from Go (max 15 digits). Reject if it looks like a LID number.
            _raw_phone = channel_cfg.get("phone_number") or ""
            _clean_raw_phone = _raw_phone.lstrip("+")
            resolved_phone = _raw_phone if (_clean_raw_phone and len(_clean_raw_phone) <= 15) else ""
            raw_jid = channel_cfg.get("user_phone") or session.external_user_id or str(session.id)
            clean_jid = _normalize_jid(raw_jid)
            is_lid = raw_jid and "@lid" in raw_jid
            customer_name = sender_name or channel_cfg.get("sender_name") or ""
            if resolved_phone:
                # Nomor asli tersedia — tampilkan langsung
                clean_phone = _normalize_jid(resolved_phone)
                user_phone_display = clean_phone.lstrip("+")
            elif is_lid:
                # LID tanpa phone_number — tampilkan LID apa adanya (tidak strip @lid)
                user_phone_display = raw_jid
            else:
                user_phone_display = clean_jid.lstrip("+") if clean_jid else "(tidak diketahui)"

            clean_reason = _clean_jid_from_text(reason)
            clean_summary = _clean_jid_from_text(summary)
            pesan_customer = clean_summary or clean_reason or "(tidak ada ringkasan pesan)"

            notif_text = (
                f"ESKALASI PESAN DARI CUSTOMER\n"
                f"ID Kasus: {case_id}\n"
                f"Nomor customer/user: {user_phone_display}\n"
                + (f"Nama customer: {customer_name}\n" if customer_name else "")
                + f"Alasan eskalasi: {clean_reason}\n"
                + f"Pesan: {pesan_customer}\n\n"
                f"Cara balas customer:\n"
                f"Reply pesan ini di WhatsApp, lalu tulis jawaban untuk customer.\n"
                f"Agent akan mengirim balasan ke nomor customer di atas."
            )

            db.add(Message(
                session_id=session_id,
                role="escalation",
                content=notif_text,
                step_index=9000,
            ))

            # Simpan case_id di metadata_ session agar bisa di-lookup saat operator reply
            sess_meta = dict(session.metadata_ or {})
            sess_meta["escalation_case_id"] = case_id
            sess_meta["escalation_customer_phone"] = user_phone_display
            session.metadata_ = sess_meta

            await db.commit()

        try:
            from app.core.infra.channel_service import send_message
            send_result = await send_message(
                channel_type=operator_channel,
                channel_config=operator_config,
                text=notif_text,
            )
            logger.info("escalation_tool.notified_operator", reason=reason, operator=operator_phone)

            sent_message_ids: list[str] = []
            if isinstance(send_result, dict) and send_result.get("message_id"):
                sent_message_ids.append(str(send_result["message_id"]))
                await _persist_escalation_message_ids(sent_message_ids)

            sess_meta = dict(session.metadata_ or {})
            media_meta = sess_meta.get("last_incoming_media") if isinstance(sess_meta, dict) else None
            current_turn_media = sess_meta.get("current_turn_media") if isinstance(sess_meta, dict) else None
            if (
                operator_channel == "whatsapp"
                and operator_phone
                and isinstance(media_meta, dict)
                and not media_meta.get("from_operator")
                and _is_current_turn_media(media_meta, current_turn_media)
            ):
                media_type = media_meta.get("media_type")
                workspace_path = media_meta.get("workspace_path")
                filename = media_meta.get("filename") or "lampiran"
                mimetype = media_meta.get("mimetype") or "application/octet-stream"
                if workspace_path and Path(workspace_path).exists():
                    import base64
                    from app.core.infra.wa_client import send_wa_document, send_wa_image

                    encoded = base64.b64encode(Path(workspace_path).read_bytes()).decode()
                    caption = f"Lampiran dari customer untuk kasus {case_id}"
                    device_id = operator_config.get("device_id", "")
                    if media_type in ("image", "sticker"):
                        media_result = await send_wa_image(device_id, operator_phone, encoded, caption, mimetype)
                    elif media_type == "document":
                        media_result = await send_wa_document(device_id, operator_phone, encoded, filename, caption, mimetype)
                    else:
                        media_result = None
                        logger.info("escalation_tool.media_not_forwarded_type", media_type=media_type)
                    if isinstance(media_result, dict) and media_result.get("message_id"):
                        sent_message_ids.append(str(media_result["message_id"]))
                        await _persist_escalation_message_ids(sent_message_ids)
                    logger.info(
                        "escalation_tool.forwarded_media_to_operator",
                        media_type=media_type,
                        filename=filename,
                        operator=operator_phone,
                    )
        except Exception as exc:
            logger.warning("escalation_tool.channel_send_skipped", error=str(exc))

        return (
            "Eskalasi berhasil. Notifikasi ke operator SUDAH terkirim otomatis oleh tool ini — "
            "JANGAN tulis pesan tambahan ke operator apapun. "
            "Tugasmu sekarang: balas USER (bukan operator) dengan 1-2 kalimat singkat bahwa pertanyaannya "
            "sedang diteruskan ke tim yang berwenang dan akan segera dibalas. "
            "JANGAN sebutkan nomor telepon, JID, nama operator, atau detail teknis apapun."
        )

    @tool
    async def reply_to_user(message: str) -> str:
        """
        Kirim pesan final ke user.
        Panggil tool ini hanya jika operator sudah menyetujui pengiriman.
        Persetujuan eksplisit termasuk: "kirim", "ok kirim", "langsung kirim",
        "rapihin aja pesannya terus kirim", atau instruksi sejenis yang jelas meminta pesan dikirim.
        Jika operator belum meminta kirim, tampilkan draft dulu dan tunggu konfirmasi.
        Args: message (pesan final yang akan dikirim ke user).
        """
        from app.models.message import Message as Msg

        channel_type_val: str = "whatsapp"
        ch_cfg: dict = {}

        message = _clean_jid_from_text(message)
        if not allow_reply_to_user or not user_jid:
            logger.warning(
                "escalation_tool.reply_to_user.blocked_without_route",
                session_id=str(session_id),
            )
            return "[error] reply_to_user diblokir karena tidak ada route eskalasi customer yang valid."

        async with db_factory() as db:
            sess_result = await db.execute(select(Session).where(Session.id == session_id))
            op_session = sess_result.scalar_one_or_none()
            ch_cfg = dict(op_session.channel_config or {}) if op_session else {}
            operator_target = ch_cfg.get("user_phone") or (op_session.external_user_id if op_session else "")
            if _normalize_jid(str(operator_target)).lstrip("+") == _normalize_jid(user_jid).lstrip("+"):
                logger.warning(
                    "escalation_tool.reply_to_user.blocked_operator_target",
                    session_id=str(session_id),
                )
                return "[error] Target customer sama dengan operator; pengiriman diblokir."
            ch_cfg["user_phone"] = user_jid
            channel_type_val = op_session.channel_type if op_session else "whatsapp"

        # PENTING: gunakan user_jid langsung dari closure — JANGAN load session.channel_config
        # karena di sesi operator, channel_config.user_phone = JID operator (bukan user).
        try:
            from app.core.infra.channel_service import send_message
            await send_message(
                channel_type=channel_type_val,
                channel_config=ch_cfg,
                text=message,
            )
            logger.info("escalation_tool.reply_to_user.sent", target=user_jid)
        except Exception as exc:
            logger.warning("escalation_tool.reply_channel_failed", error=str(exc))
            return f"[error] Gagal mengirim balasan ke customer: {exc}"

        async with db_factory() as db:
            db.add(Msg(
                session_id=session_id,
                role="agent",
                content=f"[TO_USER] {message}",
                step_index=9001,
            ))
            await db.commit()

        return f"[SENT_TO_USER] {message}"

    @tool
    async def send_to_number(phone_or_target: str, message: str) -> str:
        """
        Kirim pesan ke nomor telepon atau target lain (berbeda dari user utama).
        Bisa dipakai saat user utama eksplisit meminta agent mengirim WhatsApp
        ke nomor lain, atau saat operator memerintahkan agent menghubungi pihak
        lain. Jangan dipakai untuk membalas user utama dalam sesi normal.

        Args:
            phone_or_target: Nomor tujuan (contoh: "+62812xxx") atau chat_id
            message        : Pesan yang akan dikirim
        """
        from app.models.message import Message as Msg

        message = _clean_jid_from_text(message)
        if _looks_like_media_delivery_text(message):
            return (
                "[send_to_number blocked] Pesan ini mengklaim pengiriman file/dokumen/gambar. "
                "send_to_number hanya untuk teks. Untuk file gunakan send_whatsapp_document/send_whatsapp_image."
            )
        channel_type_val: str | None = None
        channel_config_val: dict = {}

        async with db_factory() as db:
            sess_result = await db.execute(select(Session).where(Session.id == session_id))
            session = sess_result.scalar_one_or_none()
            if session:
                channel_type_val = session.channel_type
                channel_config_val = session.channel_config if isinstance(session.channel_config, dict) else {}

            if channel_type_val == "whatsapp":
                from app.core.engine.wa_outbound_guard import (
                    check_wa_outbound_direct_window,
                    looks_like_outbound_wa_spam_request,
                    wa_outbound_block_reply,
                )

                if looks_like_outbound_wa_spam_request(user_message):
                    logger.warning(
                        "escalation_tool.send_to_number.blocked_spam_request",
                        target=phone_or_target,
                    )
                    return f"[send_to_number blocked] {wa_outbound_block_reply('spam_request')}"
                allowed, count = await check_wa_outbound_direct_window(
                    device_id=str(channel_config_val.get("device_id", "") or ""),
                    target=phone_or_target,
                )
                if not allowed:
                    logger.warning(
                        "escalation_tool.send_to_number.blocked_rate_limit",
                        target=phone_or_target,
                        count=count,
                    )
                    return f"[send_to_number blocked] {wa_outbound_block_reply('rate_limit')}"

            db.add(Msg(
                session_id=session_id,
                role="agent",
                content=f"[TO_NUMBER:{phone_or_target}] {message}",
                step_index=9002,
            ))
            await db.commit()

        if channel_type_val:
            try:
                from app.core.infra.channel_service import send_message
                await send_message(
                    channel_type=channel_type_val,
                    channel_config=channel_config_val,
                    text=message,
                    to_override=phone_or_target,
                )
                logger.info("escalation_tool.send_to_number", target=phone_or_target)
            except Exception as exc:
                logger.warning("escalation_tool.send_to_number_channel_failed", error=str(exc))
        else:
            logger.info("escalation_tool.send_to_number.dev_mode", target=phone_or_target, message_preview=message[:80])

        return f"[SENT_TO_NUMBER:{phone_or_target}] {message}"

    tools = [escalate_to_human, send_to_number]
    if allow_reply_to_user and user_jid:
        tools.insert(1, reply_to_user)
    return tools
